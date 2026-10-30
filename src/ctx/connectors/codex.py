"""Codex CLI connector (source name: "codex").

Reads ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl (and .jsonl.zst when the
optional `zstandard` package is installed; otherwise those files are skipped with
a warning). All Codex-specific logic lives here and only here (ADR-007).

- structural pin (`codex-rollout-v1`): the first line of every rollout must be
  `{"type": "session_meta", "payload": {"id": ...}}`. Older rollouts without that
  header (or any other shape) raise ConnectorError before anything is yielded.
- path denylist (auth.json, config) enforced for the source path and every file.
- unknown record types are counted in `last_stats`, never fatal.

Run `python -m ctx.connectors.codex` for a read-only dry-run report.
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
    zstandard_module,
)
from ctx.core.blobs import BlobStore
from ctx.core.model import RawEvent, RawSession
from ctx.core.redact import redact_file_text_counted
from ctx.core.sync_state import SyncState, fingerprint

SOURCE = "codex"
SOURCE_ROOT = Path.home() / ".codex"
ALLOWED_PATHS = (SOURCE_ROOT,)
DENYLIST_PATHS = (
    "auth.json",
    "config.toml",
    "config.json",
    "installation_id",
    "*.sqlite",
    "*.sqlite-wal",
    ".env",
    ".env.*",
)
DENYLIST_TABLES: frozenset[str] = frozenset()
SCHEMA_VERSION_PIN = "codex-rollout-v1"

_KNOWN_SKIPPED_TYPES = frozenset({"event_msg", "turn_context", "session_meta", "compacted"})
_SYNTHETIC_PREFIXES = ("<environment_context>", "<user_instructions>", "# AGENTS.md instructions")
_TOOL_CALL_TYPES = {"function_call": "arguments", "custom_tool_call": "input"}
_TOOL_OUTPUT_TYPES = frozenset(
    {"function_call_output", "custom_tool_output", "custom_tool_call_output"}
)


def _meta(first: Any) -> dict | None:
    """The session_meta payload if `first` is a valid header record."""
    if (
        isinstance(first, dict)
        and first.get("type") == "session_meta"
        and isinstance(first.get("payload"), dict)
        and isinstance(first["payload"].get("id"), str)
        and first["payload"]["id"]
    ):
        return first["payload"]
    return None


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif isinstance(item, dict) and "image" in str(item.get("type")):
                parts.append("[image]")
        return "\n".join(parts)
    return ""


def _output_text(output: Any) -> str:
    if isinstance(output, str):
        return output
    if isinstance(output, dict):
        for key in ("content", "output"):
            if isinstance(output.get(key), str):
                return output[key]
        return json.dumps(output, ensure_ascii=False, sort_keys=True)
    return _text_of(output)


class CodexConnector:
    name = SOURCE
    schema_version_pin = SCHEMA_VERSION_PIN
    denylist_paths = DENYLIST_PATHS
    denylist_tables = DENYLIST_TABLES

    def __init__(self, source_root: Path | None = None) -> None:
        self.source_root = Path(source_root) if source_root else SOURCE_ROOT
        self.allowed_paths = [self.source_root]
        self.last_stats = IngestStats()

    def default_source_path(self) -> Path:
        return self.source_root / "sessions"

    def _files(self, source_path: Path, stats: IngestStats | None = None) -> list[Path]:
        assert_path_allowed(source_path, self.source_root, self.denylist_paths)
        path = Path(source_path)
        if not path.exists():
            raise ConnectorError(f"Codex sessions not found: {path}")
        if path.is_file():
            candidates = [path]
        else:
            candidates = sorted(
                set(path.glob("**/rollout-*.jsonl")) | set(path.glob("**/rollout-*.jsonl.zst"))
            )
        files = []
        for f in candidates:
            assert_path_allowed(f, self.source_root, self.denylist_paths)
            if f.name.endswith(".zst") and zstandard_module() is None:
                if stats is not None:
                    stats.skipped["zst_without_zstandard"] += 1
                    stats.warn(
                        "skipped .jsonl.zst rollouts: install the optional 'zstandard' package"
                    )
                continue
            files.append(f)
        return files

    def check_version(self, source_path: Path) -> str:
        files = self._files(source_path)
        cli_version: str | None = None
        problems: list[str] = []
        for f in files:
            first = next(((n, o) for n, _r, o in read_records(f)), None)
            if first is None:
                continue  # empty rollout: nothing to import
            meta = _meta(first[1])
            if meta is None:
                problems.append(
                    f"{f.name}: first line is not a session_meta record (older or unknown format)"
                )
                break
            if cli_version is None and isinstance(meta.get("cli_version"), str):
                cli_version = meta["cli_version"]
        if problems:
            raise ConnectorError(
                "Codex rollout format unrecognized; failing loud, nothing was written: "
                + "; ".join(problems)
            )
        if not files:
            return f"{SCHEMA_VERSION_PIN}:empty"
        return f"{SCHEMA_VERSION_PIN}:cli-{cli_version}" if cli_version else SCHEMA_VERSION_PIN

    def fetch(
        self,
        source_path: Path,
        *,
        blob_store: BlobStore,
        tmp_dir: Path,
        state: SyncState | None = None,
    ) -> Iterator[RawSession | RawEvent]:
        self.check_version(source_path)
        stats = self.last_stats = IngestStats()
        files = self._files(source_path, stats)
        stats.files = len(files)
        state = state or SyncState()
        prints = {f: fingerprint(f) for f in files}  # before reading (ADR-024)
        files = [f for f in files if not state.is_unchanged(f"file:{f}", prints[f])]
        sessions = [s for f in files if (s := self._session(f)) is not None]
        sessions.sort(key=lambda s: (s.started_at, s.source_session_id))
        by_id = {s.source_session_id: s for s in sessions}
        yield from by_id.values()
        seen: set[str] = set()
        for f in files:
            yield from self._events(f, blob_store, stats, seen)
            state.stage(f"file:{f}", prints[f])

    def _session(self, path: Path) -> RawSession | None:
        meta = None
        first_ts = last_ts = None
        cwd = model = None
        for n, _raw, obj in read_records(path):
            if n and meta is None:
                meta = _meta(obj)
                if meta is None:
                    return None
                cwd = meta.get("cwd") if isinstance(meta.get("cwd"), str) else None
                first_ts = parse_ts(meta.get("timestamp")) or parse_ts(obj.get("timestamp"))
                continue
            if not isinstance(obj, dict):
                continue
            ts = parse_ts(obj.get("timestamp"))
            if ts is not None:
                last_ts = ts
                first_ts = first_ts if first_ts is not None else ts
            payload = obj.get("payload")
            if obj.get("type") == "turn_context" and isinstance(payload, dict):
                if isinstance(payload.get("model"), str):
                    model = payload["model"]
                if cwd is None and isinstance(payload.get("cwd"), str):
                    cwd = payload["cwd"]
        if meta is None:
            return None
        start = first_ts or 0
        return RawSession(
            source=SOURCE,
            source_session_id=meta["id"],
            started_at=start,
            ended_at=last_ts or start,
            cwd=cwd,
            model=model,
            agent=meta.get("originator") if isinstance(meta.get("originator"), str) else None,
            title=None,
        )

    def _events(
        self, path: Path, blob_store: BlobStore, stats: IngestStats, seen: set[str]
    ) -> Iterator[RawEvent]:
        sid: str | None = None
        start_ts = 0
        sensitive_calls: dict[str, str] = {}
        for line_no, raw, obj in read_records(path, stats):
            if obj is None:
                continue
            stats.lines += 1
            if not isinstance(obj, dict):
                stats.skipped["non_object_line"] += 1
                continue
            t = obj.get("type")
            stats.lines_by_type[str(t)] += 1
            payload = obj.get("payload")
            if sid is None:
                meta = _meta(obj)
                if meta is None:
                    return
                sid = meta["id"]
                start_ts = parse_ts(meta.get("timestamp")) or parse_ts(obj.get("timestamp")) or 0
                continue
            if t != "response_item":
                reason = "bookkeeping" if t in _KNOWN_SKIPPED_TYPES else "unknown_type"
                stats.skipped[f"{reason}:{t}"] += 1
                continue
            if not isinstance(payload, dict):
                stats.skipped["malformed_record"] += 1
                continue
            ts = parse_ts(obj.get("timestamp")) or start_ts
            mapped = self._map_item(payload, stats, sensitive_calls)
            if mapped is None:
                continue
            role, kind, content, call_id, synthetic, counts = mapped
            source_id = f"{sid}:{line_no}"
            if source_id in seen:
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
                hash_basis=raw,
                message_ref=payload["id"] if isinstance(payload.get("id"), str) else source_id,
                blob_store=blob_store,
                stats=stats,
                file_counts=counts,
            )

    def _map_item(
        self, p: dict, stats: IngestStats, sensitive_calls: dict[str, str]
    ) -> tuple[str | None, str, str, str | None, bool, dict[str, int]] | None:
        """response_item payload -> (role, kind, text, call_id, synthetic, file_counts)."""
        ptype = p.get("type")
        call_id = p.get("call_id") if isinstance(p.get("call_id"), str) else None
        if ptype == "message":
            text = _text_of(p.get("content"))
            if not text.strip():
                stats.skipped["empty_block"] += 1
                return None
            role = p.get("role")
            synthetic = role not in ("user", "assistant") or text.lstrip().startswith(
                _SYNTHETIC_PREFIXES
            )
            return (
                (role if role in ("user", "assistant") else None),
                "text",
                text,
                None,
                synthetic,
                {},
            )
        if ptype == "reasoning":
            summary = p.get("summary")
            text = _text_of(summary) if isinstance(summary, list) else ""
            if not text.strip():
                stats.skipped["empty_block"] += 1  # encrypted-only reasoning
                return None
            return "assistant", "reasoning", text, None, False, {}
        if ptype in _TOOL_CALL_TYPES or ptype == "local_shell_call":
            if ptype == "local_shell_call":
                name, args = "local_shell", p.get("action")
            else:
                name = str(p.get("name") or "unknown")
                args = p.get(_TOOL_CALL_TYPES[ptype])
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        pass  # keep the raw string
            sensitive = sensitive_path_in(flatten_strings(args))
            if sensitive and call_id:
                sensitive_calls[call_id] = sensitive
            return "assistant", f"tool:{name}", tool_input_text(args), call_id, False, {}
        if ptype in _TOOL_OUTPUT_TYPES:
            text = _output_text(p.get("output"))
            if not text.strip():
                stats.skipped["empty_block"] += 1
                return None
            counts: dict[str, int] = {}
            if call_id in sensitive_calls:
                text, counts = redact_file_text_counted(sensitive_calls[call_id], text)
            return "user", "tool_result", text, call_id, False, counts
        stats.skipped[f"unknown_item:{ptype}"] += 1
        return None


if __name__ == "__main__":
    print_report(dry_run_report(CodexConnector()))
