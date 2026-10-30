"""Devin CLI connector (source name: "devin").

Reads the local session store `<devin>/cli/sessions.db` (SQLite, WAL; unofficial
format, see docs/CONNECTORS.md). Cloud Devin sessions are not stored locally and are
out of scope. All Devin-specific logic lives here only (ADR-007).

- Queried tables: `sessions`, `message_nodes`, `refinery_schema_history` (version
  marker). Everything else (FTS shadow tables, `tool_call_state`, `prompt_history`,
  `app_state`, ...) is denylisted; `tool_call_state` only duplicates what the
  message nodes already carry.
- `transcripts/<id>.json` (ATIF export of the same data) is not read: the DB is the
  complete source and the files exist for only some sessions.
- `message_nodes` is a tree that is re-inserted on compaction/branching: the same
  `message_id` appears in many nodes (observed ~2.8x). The first node (lowest
  `node_id`) of each `(session, message_id)` wins, because events are immutable.
- Structural pin `devin-sqlite-v1`; anything else raises ConnectorError before any
  record. Content is redacted first, then oversized content spills to a blob.

Run `python -m ctx.connectors.devin` for a read-only dry-run report.
"""

import json
import os
import sqlite3
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

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
    sensitive_path_in,
    tool_input_text,
)
from ctx.connectors.sqlite_common import (
    connect_readonly,
    missing_columns,
    open_snapshot,
    table_columns,
)
from ctx.core.blobs import BlobStore
from ctx.core.model import RawEvent, RawSession
from ctx.core.redact import redact, redact_file_text_counted

SOURCE = "devin"
SCHEMA_VERSION_PIN = "devin-sqlite-v1"
_CHECK_ROWS = 200

DENYLIST_PATHS = (
    "credentials.toml",
    "credentials*",
    "*.credentials",
    "mcp/*",  # mcp/oauth/* holds OAuth tokens
    "oauth*",
    "auth.json",
    "auth*.json",
    "*token*",
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
)
_FTS_SUFFIXES = ("", "_config", "_data", "_docsize", "_idx")
DENYLIST_TABLES = frozenset(
    {f"message_search{s}" for s in _FTS_SUFFIXES}
    | {
        "app_state",
        "live_row_times",
        "prompt_history",
        "rendered_commits",
        "subagent_heads",
        "tool_call_state",
        "credentials",
        "secrets",
        "oauth_tokens",
    }
)
_REQUIRED_TABLES: dict[str, set[str]] = {
    "refinery_schema_history": {"version", "name"},
    "sessions": {"id", "working_directory", "model", "created_at", "last_activity_at", "title"},
    "message_nodes": {"session_id", "node_id", "chat_message", "created_at"},
}
_ROLES = ("user", "assistant", "tool", "system")


def default_root(
    env: Mapping[str, str] | None = None,
    platform: str | None = None,
    home: Path | None = None,
) -> Path:
    """Devin data root: ~/.local/share/devin, or %APPDATA%\\devin on Windows."""
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    home = Path.home() if home is None else home
    if platform == "win32":
        appdata = env.get("APPDATA")
        return (Path(appdata) if appdata else home / "AppData" / "Roaming") / "devin"
    return Path(env.get("XDG_DATA_HOME") or home / ".local" / "share") / "devin"


class DevinConnector:
    name = SOURCE
    schema_version_pin = SCHEMA_VERSION_PIN
    denylist_paths = DENYLIST_PATHS
    denylist_tables = DENYLIST_TABLES

    def __init__(
        self,
        source_root: Path | None = None,
        *,
        env: Mapping[str, str] | None = None,
        platform: str | None = None,
    ) -> None:
        env = os.environ if env is None else env
        override = env.get("CHISEL_SESSION_DB")
        if source_root:
            self.source_root = Path(source_root)
            self._db = self.source_root / "cli" / "sessions.db"
        elif override:  # explicit DB file: its directory is the only allowed root
            self._db = Path(override).expanduser()
            self.source_root = self._db.parent
        else:
            self.source_root = default_root(env, platform)
            self._db = self.source_root / "cli" / "sessions.db"
        self.allowed_paths = [self.source_root]
        self.last_stats = IngestStats()

    def default_source_path(self) -> Path:
        return self._db

    # ---- permissions ---------------------------------------------------------------
    def _database(self, source_path: Path) -> Path:
        assert_path_allowed(source_path, self.source_root, self.denylist_paths)
        path = Path(source_path)
        if path.is_dir():
            path = next(
                (c for c in (path / "cli" / "sessions.db", path / "sessions.db") if c.exists()),
                path / "cli" / "sessions.db",
            )
            assert_path_allowed(path, self.source_root, self.denylist_paths)
        if not path.exists():
            raise ConnectorError(f"Devin session DB not found: {path}")
        return path

    # ---- version pin ---------------------------------------------------------------
    def check_version(self, source_path: Path) -> str:
        db = self._database(source_path)
        conn = connect_readonly(db)
        try:
            problems = missing_columns(conn, _REQUIRED_TABLES, self.denylist_tables)
            if not problems:
                problems = self._node_problems(conn)
            if problems:
                raise ConnectorError(
                    "Devin schema unrecognized; failing loud, nothing was written: "
                    + "; ".join(problems)
                )
            sql = "SELECT max(version) AS v FROM refinery_schema_history"
            assert_tables_allowed(sql, self.denylist_tables)
            version = conn.execute(sql).fetchone()["v"]
            return f"{SCHEMA_VERSION_PIN}:refinery-{version}"
        finally:
            conn.close()

    def _node_problems(self, conn: sqlite3.Connection) -> list[str]:
        """Head check: JSON objects must carry message_id/role. A few unparsable rows are
        tolerated (counted at fetch time); a head with no parsable row is drift."""
        sql = (
            f"SELECT node_id, chat_message FROM message_nodes ORDER BY node_id LIMIT {_CHECK_ROWS}"
        )
        assert_tables_allowed(sql, self.denylist_tables)
        rows = valid = 0
        for row in conn.execute(sql):
            rows += 1
            try:
                obj = json.loads(row["chat_message"])
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(obj, dict):
                return [f"node {row['node_id']}: chat_message is not an object"]
            if not isinstance(obj.get("message_id"), str) or not isinstance(obj.get("role"), str):
                return [f"node {row['node_id']}: chat_message lacks message_id/role"]
            valid += 1
        if rows and not valid:
            return ["no chat_message in the first rows parses as JSON"]
        return []

    # ---- fetch -----------------------------------------------------------------------
    def fetch(
        self, source_path: Path, *, blob_store: BlobStore, tmp_dir: Path
    ) -> Iterator[RawSession | RawEvent]:
        self.check_version(source_path)  # fail loud before reading content or yielding
        db = self._database(source_path)
        stats = self.last_stats = IngestStats(files=1)
        snapshot = Path(tmp_dir) / "devin-snapshot.db"
        conn = open_snapshot(db, Path(tmp_dir), snapshot.name)
        try:
            yield from self._sessions(conn)
            yield from self._events(conn, blob_store, stats)
        finally:
            conn.close()
            snapshot.unlink(missing_ok=True)

    def _sessions(self, conn: sqlite3.Connection) -> Iterator[RawSession]:
        have = table_columns(conn, "sessions", self.denylist_tables)
        agent = "agent_mode" if "agent_mode" in have else "NULL"
        sql = (
            "SELECT id, working_directory, model, created_at, last_activity_at, title, "
            f"{agent} AS agent_mode FROM sessions ORDER BY created_at, id"
        )
        assert_tables_allowed(sql, self.denylist_tables)
        for row in conn.execute(sql):
            yield RawSession(
                source=SOURCE,
                source_session_id=row["id"],
                started_at=_ms(row["created_at"]) or 0,
                ended_at=_ms(row["last_activity_at"]),
                cwd=row["working_directory"] or None,
                model=row["model"] or None,
                agent=row["agent_mode"] or "devin",
                title=redact(row["title"] or "") or None,
            )

    def _events(
        self, conn: sqlite3.Connection, blob_store: BlobStore, stats: IngestStats
    ) -> Iterator[RawEvent]:
        sql = (
            "SELECT session_id, node_id, chat_message, created_at FROM message_nodes "
            "ORDER BY created_at, node_id"
        )
        assert_tables_allowed(sql, self.denylist_tables)
        sessions_sql = "SELECT id FROM sessions"
        assert_tables_allowed(sessions_sql, self.denylist_tables)
        known = {r["id"] for r in conn.execute(sessions_sql)}
        seen: set[tuple[str, str]] = set()
        sensitive_calls: dict[str, str] = {}
        for row in conn.execute(sql):
            stats.lines += 1
            try:
                obj = json.loads(row["chat_message"])
            except (TypeError, json.JSONDecodeError):
                obj = None
            if not isinstance(obj, dict) or not isinstance(obj.get("message_id"), str):
                stats.malformed_lines += 1
                continue
            role = obj.get("role")
            stats.lines_by_type[str(role)] += 1
            if row["session_id"] not in known:
                stats.skipped["orphan_node"] += 1
                continue
            if role not in _ROLES:
                stats.skipped[f"unknown_role:{role}"] += 1
                continue
            key = (row["session_id"], obj["message_id"])
            if key in seen:
                stats.skipped["duplicate_node"] += 1
                continue
            seen.add(key)
            ts = _node_ts(obj, row["created_at"])
            ref = f"{row['session_id']}:{obj['message_id']}"
            for block, kind, content, call_id, counts in self._blocks(obj, sensitive_calls, stats):
                yield build_event(
                    source=SOURCE,
                    source_id=f"{ref}:{block}",
                    ts=ts,
                    session_ref=row["session_id"],
                    role=role if role in ("user", "assistant") and kind != "tool_result" else None,
                    kind=kind,
                    call_id=call_id,
                    synthetic=role == "system",
                    content=content,
                    hash_basis=f"{role}:{block}:{content}",
                    message_ref=obj["message_id"],
                    blob_store=blob_store,
                    stats=stats,
                    file_counts=counts,
                )

    def _blocks(
        self, obj: dict, sensitive_calls: dict[str, str], stats: IngestStats
    ) -> Iterator[tuple[str, str, str, str | None, dict[str, int]]]:
        role = obj["role"]
        content = obj.get("content") if isinstance(obj.get("content"), str) else ""
        if role == "tool":
            call_id = obj.get("tool_call_id") if isinstance(obj.get("tool_call_id"), str) else None
            if not content.strip():
                stats.skipped["empty_block"] += 1
                return
            counts: dict[str, int] = {}
            if call_id in sensitive_calls:
                content, counts = redact_file_text_counted(sensitive_calls[call_id], content)
            yield "result", "tool_result", content, call_id, counts
            return
        thinking = obj.get("thinking")
        if isinstance(thinking, str) and thinking.strip():
            yield "thinking", "reasoning", thinking, None, {}
        if content.strip():
            yield "text", "text", content, None, {}
        images = obj.get("images")
        for i, _img in enumerate(images if isinstance(images, list) else []):
            yield f"image{i}", "file", "[image]", None, {}  # bytes are never stored
        calls = obj.get("tool_calls")
        for i, call in enumerate(calls if isinstance(calls, list) else []):
            if not isinstance(call, dict):
                stats.skipped["bad_tool_call"] += 1
                continue
            call_id = call.get("id") if isinstance(call.get("id"), str) else None
            args = call.get("arguments")
            sensitive = sensitive_path_in(flatten_strings(args))
            if sensitive and call_id:
                sensitive_calls[call_id] = sensitive
            yield (
                f"call{i}",
                f"tool:{call.get('name') or 'unknown'}",
                tool_input_text(args),
                call_id,
                {},
            )


def _ms(value: Any) -> int | None:
    """INTEGER epoch seconds (Devin) -> epoch ms."""
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        return None
    return int(value * 1000)


def _node_ts(obj: dict[str, Any], fallback_seconds: Any) -> int:
    meta = obj.get("metadata")
    iso = meta.get("created_at") if isinstance(meta, dict) else None
    return parse_ts(iso) or _ms(fallback_seconds) or 0


if __name__ == "__main__":
    print_report(dry_run_report(DevinConnector()))
