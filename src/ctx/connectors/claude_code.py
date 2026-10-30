"""Claude Code connector (source name: "claude_code").

Reads ~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl (append-only JSONL
transcripts; unofficial format, see docs/CONNECTORS.md). All Claude Code-specific
logic lives here and only here (ADR-007).

- structural pin (`claude-code-jsonl-v1`): message records must carry
  uuid/sessionId/timestamp and a `message` object; anything else raises
  ConnectorError before a single record is yielded (ADR-011).
- path denylist (credentials, settings) is enforced for the source path and for
  every file opened; only `projects/*/*.jsonl` is ever read (ADR-014).
- unknown / bookkeeping line types, sidechains and malformed lines are counted
  in `last_stats`, never fatal.
- content is redacted first, then oversized content spills to a blob.

Run `python -m ctx.connectors.claude_code` for a read-only dry-run report.
"""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ctx.connectors.base import ConnectorError, assert_path_allowed
from ctx.connectors.jsonl_common import (
    IngestStats,
    build_event,
    dry_run_report,
    flatten_strings,
    parse_ts,
    print_report,
    read_records,
    sensitive_path_in,
    tool_input_text,
)
from ctx.core.blobs import BlobStore
from ctx.core.model import RawEvent, RawSession
from ctx.core.redact import redact_file_text_counted

SOURCE = "claude_code"
SOURCE_ROOT = Path.home() / ".claude"
ALLOWED_PATHS = (SOURCE_ROOT,)
DENYLIST_PATHS = (
    ".credentials.json",
    ".claude.json",
    "settings.json",
    "settings*.json",
    "config.json",
    "history.jsonl",
    ".env",
    ".env.*",
)
DENYLIST_TABLES: frozenset[str] = frozenset()
SCHEMA_VERSION_PIN = "claude-code-jsonl-v1"

_MESSAGE_TYPES = frozenset({"user", "assistant"})
# Observed bookkeeping line types: carry no conversation content we import.
_KNOWN_SKIPPED_TYPES = frozenset(
    {
        "summary",
        "system",
        "attachment",
        "mode",
        "permission-mode",
        "last-prompt",
        "ai-title",
        "tools-used",
        "queue-operation",
        "cost-state",
        "file-history-snapshot",
        "file-history-delta",
        "api-request",
        "api-request-blob",
        "api-request-shape",
        "atis-latch",
    }
)
_CHECK_LINES = 200  # structural pin looks at the head of every file


def _message_problem(obj: dict) -> str | None:
    """Why a user/assistant record is structurally unusable (None = fine)."""
    if not isinstance(obj.get("uuid"), str) or not obj["uuid"]:
        return "missing uuid"
    if not isinstance(obj.get("sessionId"), str) or not obj["sessionId"]:
        return "missing sessionId"
    if parse_ts(obj.get("timestamp")) is None:
        return "missing/invalid timestamp"
    message = obj.get("message")
    if not isinstance(message, dict) or "content" not in message:
        return "missing message.content"
    return None


class _SessionAcc:
    __slots__ = ("cwd", "end", "model", "start", "title")

    def __init__(self, ts: int) -> None:
        self.start = self.end = ts
        self.cwd: str | None = None
        self.model: str | None = None
        self.title: str | None = None


class ClaudeCodeConnector:
    name = SOURCE
    schema_version_pin = SCHEMA_VERSION_PIN
    denylist_paths = DENYLIST_PATHS
    denylist_tables = DENYLIST_TABLES

    def __init__(self, source_root: Path | None = None) -> None:
        self.source_root = Path(source_root) if source_root else SOURCE_ROOT
        self.allowed_paths = [self.source_root]
        self.last_stats = IngestStats()

    def default_source_path(self) -> Path:
        return self.source_root / "projects"

    # ---- file discovery / permissions -------------------------------------------
    def _files(self, source_path: Path) -> list[Path]:
        assert_path_allowed(source_path, self.source_root, self.denylist_paths)
        path = Path(source_path)
        if not path.exists():
            raise ConnectorError(f"Claude Code transcripts not found: {path}")
        if path.is_file():
            files = [path]
        else:
            files = sorted(set(path.glob("*.jsonl")) | set(path.glob("*/*.jsonl")))
        for f in files:
            assert_path_allowed(f, self.source_root, self.denylist_paths)
        return files

    # ---- version pin ----------------------------------------------------------------
    def check_version(self, source_path: Path) -> str:
        files = self._files(source_path)
        cli_version: str | None = None
        problems: list[str] = []
        for f in files:
            parsed_any = nonempty = 0
            for i, (line_no, _raw, obj) in enumerate(read_records(f)):
                if i >= _CHECK_LINES:
                    break
                nonempty += 1
                if obj is None:
                    continue
                if not isinstance(obj, dict):
                    problems.append(f"{f.name}:{line_no} line is not a JSON object")
                    break
                parsed_any += 1
                if obj.get("type") in _MESSAGE_TYPES:
                    why = _message_problem(obj)
                    if why:
                        problems.append(f"{f.name}:{line_no} {obj['type']} record {why}")
                        break
                    if cli_version is None and isinstance(obj.get("version"), str):
                        cli_version = obj["version"]
            if nonempty and not parsed_any and not problems:
                problems.append(f"{f.name}: no line parses as JSON")
            if problems:
                break
        if problems:
            raise ConnectorError(
                "Claude Code transcript format unrecognized; failing loud, nothing was written: "
                + "; ".join(problems)
            )
        if not files:
            return f"{SCHEMA_VERSION_PIN}:empty"
        return f"{SCHEMA_VERSION_PIN}:cli-{cli_version}" if cli_version else SCHEMA_VERSION_PIN

    # ---- fetch ---------------------------------------------------------------------------
    def fetch(
        self, source_path: Path, *, blob_store: BlobStore, tmp_dir: Path
    ) -> Iterator[RawSession | RawEvent]:
        self.check_version(source_path)  # fail loud before reading content or yielding
        files = self._files(source_path)
        stats = self.last_stats = IngestStats(files=len(files))
        yield from self._sessions(files)
        seen: set[str] = set()
        for f in files:
            yield from self._events(f, blob_store, stats, seen)

    def _sessions(self, files: list[Path]) -> Iterator[RawSession]:
        sessions: dict[str, _SessionAcc] = {}
        for f in files:
            for _n, _raw, obj in read_records(f):
                if not isinstance(obj, dict):
                    continue
                t = obj.get("type")
                sid = obj.get("sessionId")
                if t == "ai-title" and isinstance(sid, str) and sid in sessions:
                    title = obj.get("aiTitle")
                    if isinstance(title, str) and title:
                        sessions[sid].title = title
                    continue
                if t == "summary" and isinstance(obj.get("summary"), str):
                    continue  # summaries point at other sessions by leafUuid; not session data
                if t not in _MESSAGE_TYPES or obj.get("isSidechain") or _message_problem(obj):
                    continue
                ts = parse_ts(obj["timestamp"])
                assert ts is not None
                acc = sessions.setdefault(obj["sessionId"], _SessionAcc(ts))
                acc.start, acc.end = min(acc.start, ts), max(acc.end, ts)
                if acc.cwd is None and isinstance(obj.get("cwd"), str):
                    acc.cwd = obj["cwd"]
                model = obj["message"].get("model")
                if t == "assistant" and isinstance(model, str) and not model.startswith("<"):
                    acc.model = model
        for sid, acc in sorted(sessions.items(), key=lambda kv: (kv[1].start, kv[0])):
            yield RawSession(
                source=SOURCE,
                source_session_id=sid,
                started_at=acc.start,
                ended_at=acc.end,
                cwd=acc.cwd,
                model=acc.model,
                agent=None,
                title=acc.title,
            )

    def _events(
        self, path: Path, blob_store: BlobStore, stats: IngestStats, seen: set[str]
    ) -> Iterator[RawEvent]:
        sensitive_calls: dict[str, str] = {}
        for _line_no, raw, obj in read_records(path, stats):
            if obj is None:
                continue
            stats.lines += 1
            if not isinstance(obj, dict):
                stats.skipped["non_object_line"] += 1
                continue
            t = obj.get("type")
            stats.lines_by_type[str(t)] += 1
            if t not in _MESSAGE_TYPES:
                reason = "bookkeeping" if t in _KNOWN_SKIPPED_TYPES else "unknown_type"
                stats.skipped[f"{reason}:{t}"] += 1
                continue
            if obj.get("isSidechain"):
                stats.skipped["sidechain"] += 1
                continue
            if _message_problem(obj):
                stats.skipped["malformed_record"] += 1
                continue
            yield from self._message_events(obj, raw, blob_store, stats, seen, sensitive_calls)

    def _message_events(
        self,
        obj: dict,
        raw: str,
        blob_store: BlobStore,
        stats: IngestStats,
        seen: set[str],
        sensitive_calls: dict[str, str],
    ) -> Iterator[RawEvent]:
        uuid, sid = obj["uuid"], obj["sessionId"]
        ts = parse_ts(obj["timestamp"])
        assert ts is not None
        message = obj["message"]
        role = message.get("role") if message.get("role") in ("user", "assistant") else obj["type"]
        synthetic = bool(obj.get("isMeta") or obj.get("isCompactSummary"))
        for idx, (kind, content, call_id, file_counts, basis) in enumerate(
            self._blocks(message.get("content"), stats, sensitive_calls)
        ):
            source_id = f"{uuid}:{idx}"
            if source_id in seen:  # resumed sessions copy earlier lines into a new file
                stats.skipped["duplicate_in_run"] += 1
                continue
            seen.add(source_id)
            yield build_event(
                source=SOURCE,
                source_id=source_id,
                ts=ts,
                session_ref=sid,
                role=role,
                kind=kind,
                call_id=call_id,
                synthetic=synthetic,
                content=content,
                hash_basis=f"{role}:{idx}:{basis}",
                message_ref=uuid,
                blob_store=blob_store,
                stats=stats,
                file_counts=file_counts,
            )

    def _blocks(
        self, content: Any, stats: IngestStats, sensitive_calls: dict[str, str]
    ) -> Iterator[tuple[str, str, str | None, dict[str, int], str]]:
        """Yield (kind, text, call_id, file_redaction_counts, hash_basis) per block.

        Indices are positional so ids stay stable even when a block is skipped.
        """
        if isinstance(content, str):
            if content.strip():
                yield "text", content, None, {}, content
            return
        if not isinstance(content, list):
            stats.skipped["bad_content_shape"] += 1
            return
        for idx, block in enumerate(content):
            if not isinstance(block, dict):
                stats.skipped["bad_block"] += 1
                continue
            btype = str(block.get("type"))
            basis = json.dumps(block, ensure_ascii=False, sort_keys=True)
            if btype == "text":
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    yield "text", text, None, {}, basis
                else:
                    stats.skipped["empty_block"] += 1
            elif btype == "thinking":
                text = block.get("thinking")
                if isinstance(text, str) and text.strip():
                    yield "reasoning", text, None, {}, basis
                else:
                    stats.skipped["empty_block"] += 1  # signature-only thinking blocks
            elif btype in ("tool_use", "server_tool_use"):
                tool_input = block.get("input")
                call_id = block.get("id") if isinstance(block.get("id"), str) else None
                sensitive = sensitive_path_in(flatten_strings(tool_input))
                if sensitive and call_id:
                    sensitive_calls[call_id] = sensitive
                name = str(block.get("name") or "unknown")
                yield f"tool:{name}", tool_input_text(tool_input), call_id, {}, basis
            elif btype == "tool_result":
                call_id = block.get("tool_use_id")
                call_id = call_id if isinstance(call_id, str) else None
                text = self._result_text(block.get("content"))
                if not text.strip():
                    stats.skipped["empty_block"] += 1
                    continue
                counts: dict[str, int] = {}
                if call_id in sensitive_calls:
                    text, counts = redact_file_text_counted(sensitive_calls[call_id], text)
                yield "tool_result", text, call_id, counts, basis
            elif btype == "image":
                source = block.get("source")
                media = source.get("media_type") if isinstance(source, dict) else None
                yield "file", f"[image: {media or 'unknown'}]", None, {}, f"image:{media}:{idx}"
            else:
                stats.skipped[f"unknown_block:{btype}"] += 1

    @staticmethod
    def _result_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text") or ""))
                elif isinstance(item, dict) and item.get("type") == "image":
                    parts.append("[image]")
                elif isinstance(item, str):
                    parts.append(item)
            return "\n".join(p for p in parts if p)
        return ""


if __name__ == "__main__":
    print_report(dry_run_report(ClaudeCodeConnector()))
