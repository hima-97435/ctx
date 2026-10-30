"""Antigravity CLI ("agy") connector (source name: "antigravity_cli").

Reads `~/.gemini/antigravity-cli/` (unofficial format, docs/CONNECTORS.md section 3).
This is the CLI only; the Antigravity IDE (`~/.gemini/antigravity/`) is a separate,
not-yet-built source. All agy-specific logic lives here only (ADR-007).

What is read:
- `brain/<conversation-id>/.system_generated/logs/transcript_full.jsonl`: one JSON
  step per line (the complete transcript; `transcript.jsonl` is a truncated copy).
- `conversation_summaries.db` (SQLite, plain text columns): title, workspace, agent.
- `cache/last_conversations.json` (workspace path -> latest conversation id): the
  fallback for project linking when the summaries DB lacks a workspace.

What cannot be read: `conversations/*.db` and `*.pb` hold protobuf blobs with no
public schema. They are never opened; conversations that exist only there are
counted and reported as a warning (never a silent skip, never a hard failure).

Structural pin `antigravity-cli-transcript-v1`: the head of every transcript must be
JSON objects with `step_index`, `type`, `source` and a parsable `created_at`.
Content is redacted first, then oversized content spills to a blob.

Run `python -m ctx.connectors.antigravity_cli` for a read-only dry-run report.
"""

import json
import os
from collections import deque
from collections.abc import Iterator, Mapping
from pathlib import Path, PureWindowsPath
from typing import Any
from urllib.parse import unquote, urlparse

from ctx.connectors.base import (
    ConnectorError,
    assert_path_allowed,
    assert_tables_allowed,
)
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
from ctx.connectors.sqlite_common import connect_readonly, missing_columns, open_snapshot
from ctx.core.blobs import BlobStore
from ctx.core.model import RawEvent, RawSession
from ctx.core.redact import redact, redact_file_text_counted

SOURCE = "antigravity_cli"
SCHEMA_VERSION_PIN = "antigravity-cli-transcript-v1"
SOURCE_ROOT = Path.home() / ".gemini" / "antigravity-cli"
TRANSCRIPT_GLOB = "brain/*/.system_generated/logs/transcript_full.jsonl"
SUMMARIES_DB = "conversation_summaries.db"
_CHECK_LINES = 200

DENYLIST_PATHS = (
    "history.jsonl",
    "settings.json",
    "config.json",
    "mcp_config.json",
    "plugins/**/mcp_config.json",
    "auth.json",
    "oauth*",
    "google_accounts.json",
    "credentials*",
    "*.credentials",
    "installation_id",
    "user_settings.pb",
    "jetski_state.pbtxt",
    "cli.log",
    "log/*",
    "crashes/*",
    "plugin_data/*",
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
)
# Tables of the per-conversation protobuf DBs; those files are never opened.
DENYLIST_TABLES = frozenset(
    {
        "battle_mode_infos",
        "executor_metadata",
        "gen_metadata",
        "parent_references",
        "steps",
        "trajectory_meta",
        "trajectory_metadata_blob",
    }
)
_SUMMARY_COLUMNS = {"conversation_id", "title", "workspace_uris", "agent_name"}
_RESULT_TYPES_SKIPPED = frozenset({"CONVERSATION_HISTORY"})


def default_root(home: Path | None = None) -> Path:
    """`~/.gemini/antigravity-cli` (`%USERPROFILE%\\.gemini\\antigravity-cli` on Windows)."""
    return (Path.home() if home is None else home) / ".gemini" / "antigravity-cli"


def file_uri_to_path(uri: str) -> str | None:
    """`file:///Users/me/proj` -> `/Users/me/proj`; `file:///C:/x` -> `C:/x`."""
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return None
    path = unquote(parsed.path)
    if len(path) > 2 and path[0] == "/" and path[2] == ":":
        return str(PureWindowsPath(path[1:]).as_posix())
    return path or None


class _Meta:
    __slots__ = ("agent", "cwd", "title")

    def __init__(self) -> None:
        self.title: str | None = None
        self.cwd: str | None = None
        self.agent: str | None = None


class AntigravityCliConnector:
    name = SOURCE
    schema_version_pin = SCHEMA_VERSION_PIN
    denylist_paths = DENYLIST_PATHS
    denylist_tables = DENYLIST_TABLES

    def __init__(
        self,
        source_root: Path | None = None,
        *,
        env: Mapping[str, str] | None = None,
    ) -> None:
        env = os.environ if env is None else env
        self.source_root = Path(source_root) if source_root else default_root()
        conv = env.get("AGY_CONVERSATIONS_DIR")
        self.conversations_dir = (
            Path(conv).expanduser() if conv else self.source_root / "conversations"
        )
        self.allowed_paths = [self.source_root]
        if self.conversations_dir.parent != self.source_root:
            self.allowed_paths.append(self.conversations_dir)  # names only, never opened
        self.last_stats = IngestStats()

    def default_source_path(self) -> Path:
        return self.source_root

    # ---- discovery / permissions --------------------------------------------------
    def _transcripts(self, source_path: Path) -> list[Path]:
        assert_path_allowed(source_path, self.source_root, self.denylist_paths)
        root = Path(source_path)
        if not root.is_dir():
            raise ConnectorError(f"Antigravity CLI directory not found: {root}")
        files = sorted(root.glob(TRANSCRIPT_GLOB))
        for f in files:
            assert_path_allowed(f, self.source_root, self.denylist_paths)
        return files

    def _opaque_only(self, transcript_ids: set[str]) -> list[str]:
        """Conversation ids that exist only as protobuf (names listed, files not opened)."""
        d = self.conversations_dir
        if not d.is_dir():
            return []
        ids = {p.stem for p in d.iterdir() if p.suffix in (".db", ".pb")}
        return sorted(ids - transcript_ids)

    # ---- version pin ----------------------------------------------------------------
    def check_version(self, source_path: Path) -> str:
        files = self._transcripts(source_path)
        problems: list[str] = []
        for f in files:
            for i, (line_no, _raw, obj) in enumerate(read_records(f)):
                if i >= _CHECK_LINES:
                    break
                if obj is None:
                    continue  # torn final line of a live transcript
                why = _step_problem(obj)
                if why:
                    problems.append(f"{f.parent.parent.parent.name}:{line_no} {why}")
                    break
            if problems:
                break
        if not problems:
            summaries = Path(source_path) / SUMMARIES_DB
            if summaries.exists():
                problems = self._summary_problems(summaries)
        if problems:
            raise ConnectorError(
                "Antigravity CLI format unrecognized; failing loud, nothing was written: "
                + "; ".join(problems)
            )
        return SCHEMA_VERSION_PIN if files else f"{SCHEMA_VERSION_PIN}:empty"

    def _summary_problems(self, path: Path) -> list[str]:
        assert_path_allowed(path, self.source_root, self.denylist_paths)
        conn = connect_readonly(path)
        try:
            return missing_columns(
                conn, {"conversation_summaries": _SUMMARY_COLUMNS}, self.denylist_tables
            )
        finally:
            conn.close()

    # ---- fetch -----------------------------------------------------------------------------
    def fetch(
        self, source_path: Path, *, blob_store: BlobStore, tmp_dir: Path
    ) -> Iterator[RawSession | RawEvent]:
        self.check_version(source_path)  # fail loud before reading content or yielding
        files = self._transcripts(source_path)
        stats = self.last_stats = IngestStats(files=len(files))
        ids = {_cid(f) for f in files}
        opaque = self._opaque_only(ids)
        if opaque:
            stats.skipped["protobuf_only_conversation"] += len(opaque)
            stats.warn(
                f"{len(opaque)} conversation(s) exist only as protobuf (conversations/*.db|*.pb) "
                "and cannot be imported"
            )
        meta = self._metadata(Path(source_path), ids, Path(tmp_dir))
        spans = {f: _span(f) for f in files}
        for f in sorted(files, key=lambda p: (spans[p][0], _cid(p))):
            cid = _cid(f)
            m = meta.get(cid, _Meta())
            yield RawSession(
                source=SOURCE,
                source_session_id=cid,
                started_at=spans[f][0],
                ended_at=spans[f][1],
                cwd=m.cwd,
                model=None,
                agent=m.agent or "antigravity",
                title=m.title,
            )
        seen: set[str] = set()
        for f in files:
            yield from self._events(f, blob_store, stats, seen)

    def _metadata(self, root: Path, ids: set[str], tmp_dir: Path) -> dict[str, _Meta]:
        meta: dict[str, _Meta] = {cid: _Meta() for cid in ids}
        summaries = root / SUMMARIES_DB
        if summaries.exists():
            conn = open_snapshot(summaries, tmp_dir, "agy-summaries-snapshot.db")
            try:
                sql = (
                    "SELECT conversation_id, title, workspace_uris, agent_name "
                    "FROM conversation_summaries"
                )
                assert_tables_allowed(sql, self.denylist_tables)
                for row in conn.execute(sql):
                    m = meta.get(row["conversation_id"])
                    if m is None:
                        continue
                    m.title = redact(row["title"] or "") or None
                    m.agent = row["agent_name"] or None
                    m.cwd = _first_workspace(row["workspace_uris"])
            finally:
                conn.close()
                (tmp_dir / "agy-summaries-snapshot.db").unlink(missing_ok=True)
        last = root / "cache" / "last_conversations.json"
        if last.exists():
            assert_path_allowed(last, self.source_root, self.denylist_paths)
            try:
                mapping = json.loads(last.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                mapping = {}
            if isinstance(mapping, dict):
                for workspace, cid in sorted(mapping.items()):
                    m = meta.get(cid) if isinstance(cid, str) else None
                    if m is not None and m.cwd is None and isinstance(workspace, str):
                        m.cwd = workspace
        return meta

    def _events(
        self, path: Path, blob_store: BlobStore, stats: IngestStats, seen: set[str]
    ) -> Iterator[RawEvent]:
        cid = _cid(path)
        records = [(n, o) for n, _raw, o in read_records(path, stats)]
        pending: deque[tuple[str | None, str | None]] = deque()  # (call_id, sensitive path)
        for pos, (_line_no, obj) in enumerate(records):
            if obj is None:
                continue
            stats.lines += 1
            if _step_problem(obj):
                stats.skipped["malformed_record"] += 1
                continue
            stype = obj["type"]
            stats.lines_by_type[stype] += 1
            if obj.get("status") == "RUNNING" and pos == len(records) - 1:
                stats.skipped["running_last_step"] += 1  # live; picked up complete next sync
                continue
            if stype in _RESULT_TYPES_SKIPPED:
                stats.skipped[f"empty_marker:{stype}"] += 1
                continue
            ts = parse_ts(obj["created_at"])
            assert ts is not None
            step = obj["step_index"]
            for block, kind, content, role, call_id, synthetic, counts in self._blocks(
                cid, obj, pending, stats
            ):
                source_id = f"{cid}:{step}:{block}"
                if source_id in seen:
                    stats.skipped["duplicate_in_run"] += 1
                    continue
                seen.add(source_id)
                yield build_event(
                    source=SOURCE,
                    source_id=source_id,
                    ts=ts,
                    session_ref=cid,
                    role=role,
                    kind=kind,
                    call_id=call_id,
                    synthetic=synthetic,
                    content=content,
                    hash_basis=f"{stype}:{block}:{content}",
                    message_ref=f"{cid}:{step}",
                    blob_store=blob_store,
                    stats=stats,
                    file_counts=counts,
                )

    def _blocks(
        self,
        cid: str,
        obj: dict,
        pending: "deque[tuple[str | None, str | None]]",
        stats: IngestStats,
    ) -> Iterator[tuple[str, str, str, str | None, str | None, bool, dict[str, int]]]:
        """Yield (block id, kind, text, role, call_id, synthetic, file_redaction_counts)."""
        stype, source, step = obj["type"], obj["source"], obj["step_index"]
        content = obj.get("content") if isinstance(obj.get("content"), str) else ""
        if source.startswith("USER") and stype == "USER_INPUT":
            if content.strip():
                yield "text", "text", content, "user", None, False, {}
            return
        if stype == "PLANNER_RESPONSE":
            thinking = obj.get("thinking")
            if isinstance(thinking, str) and thinking.strip():
                yield "thinking", "reasoning", thinking, "assistant", None, False, {}
            if content.strip():
                yield "text", "text", content, "assistant", None, False, {}
            calls = obj.get("tool_calls")
            for i, call in enumerate(calls if isinstance(calls, list) else []):
                if not isinstance(call, dict):
                    stats.skipped["bad_tool_call"] += 1
                    continue
                args = call.get("args")
                call_id = f"{cid}:{step}:call{i}"
                pending.append((call_id, sensitive_path_in(flatten_strings(args))))
                name = call.get("name") or "unknown"
                yield (
                    f"call{i}",
                    f"tool:{name}",
                    tool_input_text(args),
                    "assistant",
                    call_id,
                    False,
                    {},
                )
            return
        if not content.strip():
            stats.skipped["empty_block"] += 1
            return
        if source == "SYSTEM":
            yield "text", "text", content, None, None, True, {}
            return
        if source.startswith("USER"):  # e.g. a user-run command: no planner call precedes it
            yield "result", "tool_result", content, None, None, False, {}
            return
        if source == "MODEL":
            call_id, sensitive = pending.popleft() if pending else (None, None)
            counts: dict[str, int] = {}
            if sensitive:
                content, counts = redact_file_text_counted(sensitive, content)
            yield "result", "tool_result", content, None, call_id, False, counts
            return
        stats.skipped[f"unknown_source:{source}"] += 1


def _cid(transcript: Path) -> str:
    return transcript.parent.parent.parent.name


def _step_problem(obj: Any) -> str | None:
    if not isinstance(obj, dict):
        return "line is not a JSON object"
    if isinstance(obj.get("step_index"), bool) or not isinstance(obj.get("step_index"), int):
        return "missing step_index"
    if not isinstance(obj.get("type"), str) or not isinstance(obj.get("source"), str):
        return "missing type/source"
    if parse_ts(obj.get("created_at")) is None:
        return "missing/invalid created_at"
    return None


def _span(path: Path) -> tuple[int, int]:
    lo = hi = 0
    for _n, _raw, obj in read_records(path):
        if isinstance(obj, dict) and not _step_problem(obj):
            ts = parse_ts(obj["created_at"]) or 0
            lo = ts if lo == 0 else min(lo, ts)
            hi = max(hi, ts)
    return lo, hi


def _first_workspace(raw: Any) -> str | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        uris = json.loads(raw)
    except json.JSONDecodeError:
        return None
    for uri in uris if isinstance(uris, list) else []:
        if isinstance(uri, str) and (path := file_uri_to_path(uri)):
            return path
    return None


if __name__ == "__main__":
    print_report(dry_run_report(AntigravityCliConnector()))
