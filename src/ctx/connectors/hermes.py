"""Hermes Agent connector (source name: "hermes").

Reads Hermes' SQLite session store `<HERMES_HOME>/state.db` plus
`<HERMES_HOME>/profiles/*/state.db` (see docs/CONNECTORS.md for the observed schema,
unofficial). All Hermes-specific logic lives here only (ADR-007).

- Only tables `sessions`, `messages` and `schema_version` are ever queried. Every
  other table (FTS shadow tables, gateway/room/delivery state, `state_meta`,
  `system_prompts`, ...) is denylisted and asserted against on each statement.
- The DB is read via a snapshot (backup API); WAL handling is in `sqlite_common`.
- Structural pin `hermes-sqlite-v1`: required tables/columns and an integer
  `schema_version`; anything else raises ConnectorError before any record.
- Profiles are namespaced (`<profile>/<id>`) so integer message ids and session ids
  cannot collide across profile DBs. The default profile is not prefixed.
- Content is redacted first, then oversized content spills to a blob.

Run `python -m ctx.connectors.hermes` for a read-only dry-run report.
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
from ctx.core.sync_state import SyncState, fingerprint

SOURCE = "hermes"
SCHEMA_VERSION_PIN = "hermes-sqlite-v1"
DB_NAME = "state.db"

# Never opened, whatever the path (matched against the relative path and file name).
DENYLIST_PATHS = (
    ".env",
    ".env.*",
    "auth.json",
    "auth.lock",
    "config.yaml",
    "config.yml",
    "config.json",
    ".hermes_history",
    "gateway_state.json",
    "pairing/*",
    "platforms/*",
    "kanban.db*",
    "projects.db*",
    "*.pem",
    "*.key",
)

_FTS_SUFFIXES = ("", "_config", "_data", "_docsize", "_idx", "_src")
# Tables this connector must never query: FTS shadow tables (derived, duplicate
# content), gateway/room/delivery state, identity/session-routing data and anything
# credential-ish. State that is not conversation content is simply not ours to read.
DENYLIST_TABLES = frozenset(
    {f"messages_fts{s}" for s in _FTS_SUFFIXES}
    | {f"messages_fts_trigram{s}" for s in _FTS_SUFFIXES}
    | {
        "async_delegations",
        "compression_locks",
        "conversation_generations",
        "delivery_obligations",
        "gateway_heartbeats",
        "gateway_hygiene_state",
        "gateway_routing",
        "hosted_rooms",
        "hosted_room_events",
        "hosted_room_links",
        "hosted_room_peer_reservations",
        "hosted_room_policy_cursors",
        "hosted_room_policy_events",
        "hosted_room_policy_publications",
        "hosted_room_policy_threads",
        "hosted_room_policy_transcript",
        "hosted_room_policy_transcript_state",
        "hosted_room_policy_watermarks",
        "hosted_room_remote_runs",
        "hosted_room_retired_ids",
        "hosted_room_revoked_grants",
        "runtime_epoch",
        "session_admissions",
        "session_model_usage",
        "session_turn_leases",
        "state_meta",
        "system_prompts",
        "worker_executions",
        "worker_receipts",
        "credentials",
        "secrets",
        "api_keys",
        "oauth_tokens",
    }
)

_REQUIRED_TABLES: dict[str, set[str]] = {
    "schema_version": {"version"},
    "sessions": {"id", "started_at"},
    "messages": {
        "id",
        "session_id",
        "role",
        "content",
        "tool_call_id",
        "tool_calls",
        "tool_name",
        "timestamp",
    },
}
_OPTIONAL_SESSION_COLUMNS = ("model", "ended_at", "cwd", "title", "last_activity_at")
_OPTIONAL_MESSAGE_COLUMNS = ("message_uid", "reasoning_content", "reasoning", "_compressed_summary")


def default_home(
    env: Mapping[str, str] | None = None,
    platform: str | None = None,
    home: Path | None = None,
) -> Path:
    """HERMES_HOME, else %LOCALAPPDATA%\\hermes (Windows), else ~/.hermes."""
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    home = Path.home() if home is None else home
    if env.get("HERMES_HOME"):
        return Path(env["HERMES_HOME"]).expanduser()
    if platform == "win32":
        local = env.get("LOCALAPPDATA")
        return (Path(local) if local else home / "AppData" / "Local") / "hermes"
    return home / ".hermes"


def _profile_of(db: Path) -> str:
    return db.parent.name if db.parent.parent.name == "profiles" else "default"


def _ns(profile: str, value: Any) -> str:
    return str(value) if profile == "default" else f"{profile}/{value}"


class HermesConnector:
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
        self.source_root = Path(source_root) if source_root else default_home(env, platform)
        self.allowed_paths = [self.source_root]
        self.last_stats = IngestStats()

    def default_source_path(self) -> Path:
        return self.source_root

    # ---- discovery / permissions -------------------------------------------------
    def _databases(self, source_path: Path) -> list[Path]:
        assert_path_allowed(source_path, self.source_root, self.denylist_paths)
        path = Path(source_path)
        if not path.exists():
            raise ConnectorError(f"Hermes data not found: {path}")
        if path.is_file():
            dbs = [path]
        else:
            dbs = [
                d
                for d in [path / DB_NAME, *sorted(path.glob(f"profiles/*/{DB_NAME}"))]
                if d.exists()
            ]
        if not dbs:
            raise ConnectorError(f"no {DB_NAME} found under {path}")
        for db in dbs:
            assert_path_allowed(db, self.source_root, self.denylist_paths)
        return dbs

    # ---- version pin ----------------------------------------------------------------
    def _check_one(self, conn: sqlite3.Connection, db: Path) -> str:
        problems = missing_columns(conn, _REQUIRED_TABLES, self.denylist_tables)
        if problems:
            raise ConnectorError(
                f"Hermes schema unrecognized in {db}; failing loud, nothing was written: "
                + "; ".join(problems)
            )
        sql = "SELECT version FROM schema_version"
        assert_tables_allowed(sql, self.denylist_tables)
        rows = conn.execute(sql).fetchall()
        version = rows[0]["version"] if rows else None
        if len(rows) != 1 or isinstance(version, bool) or not isinstance(version, int):
            raise ConnectorError(f"Hermes schema_version in {db} is not a single integer")
        return str(version)

    def check_version(self, source_path: Path) -> str:
        versions: list[str] = []
        for db in self._databases(source_path):
            conn = connect_readonly(db)
            try:
                versions.append(self._check_one(conn, db))
            finally:
                conn.close()
        return f"{SCHEMA_VERSION_PIN}:schema-{'+'.join(sorted(set(versions)))}"

    # ---- fetch ----------------------------------------------------------------------------
    def fetch(
        self,
        source_path: Path,
        *,
        blob_store: BlobStore,
        tmp_dir: Path,
        state: SyncState | None = None,
    ) -> Iterator[RawSession | RawEvent]:
        self.check_version(source_path)  # fail loud before reading content or yielding
        dbs = self._databases(source_path)
        stats = self.last_stats = IngestStats(files=len(dbs))
        state = state or SyncState()
        prints = {db: fingerprint(db, db.with_name(db.name + "-wal")) for db in dbs}
        dbs = [db for db in dbs if not state.is_unchanged(f"db:{db}", prints[db])]
        snaps: list[tuple[str, sqlite3.Connection, Path]] = []
        try:
            for i, db in enumerate(dbs):
                name = f"hermes-snapshot-{i}.db"
                snaps.append(
                    (_profile_of(db), open_snapshot(db, Path(tmp_dir), name), Path(tmp_dir) / name)
                )
            for profile, conn, _p in snaps:  # all sessions first (engine contract)
                yield from self._sessions(conn, profile)
            for profile, conn, _p in snaps:
                yield from self._events(conn, profile, blob_store, stats)
            for db in dbs:
                state.stage(f"db:{db}", prints[db])
        finally:
            for _profile, conn, snap_path in snaps:
                conn.close()
                snap_path.unlink(missing_ok=True)

    def _sessions(self, conn: sqlite3.Connection, profile: str) -> Iterator[RawSession]:
        have = table_columns(conn, "sessions", self.denylist_tables)
        cols = ["id", "started_at", *[c for c in _OPTIONAL_SESSION_COLUMNS if c in have]]
        sql = f"SELECT {', '.join(cols)} FROM sessions ORDER BY started_at, id"
        assert_tables_allowed(sql, self.denylist_tables)
        span_sql = (
            "SELECT session_id, min(timestamp) AS lo, max(timestamp) AS hi "
            "FROM messages GROUP BY session_id"
        )
        assert_tables_allowed(span_sql, self.denylist_tables)
        spans = {r["session_id"]: (r["lo"], r["hi"]) for r in conn.execute(span_sql)}
        known: set[str] = set()
        for row in conn.execute(sql):
            known.add(row["id"])
            lo, hi = spans.get(row["id"], (None, None))
            started = _ms(row["started_at"]) or _ms(lo) or 0
            ended = None
            for key in ("ended_at", "last_activity_at"):
                if key in row.keys() and _ms(row[key]):  # noqa: SIM118 - sqlite3.Row
                    ended = _ms(row[key])
                    break
            ended = ended or _ms(hi)
            yield RawSession(
                source=SOURCE,
                source_session_id=_ns(profile, row["id"]),
                started_at=started,
                ended_at=ended,
                cwd=_opt(row, "cwd"),
                model=_opt(row, "model"),
                agent="hermes",
                title=redact(_opt(row, "title") or "") or None,
            )
        for sid, (lo, hi) in sorted(spans.items(), key=lambda kv: str(kv[0])):
            if sid is not None and sid not in known:  # messages without a session row
                yield RawSession(
                    SOURCE, _ns(profile, sid), _ms(lo) or 0, _ms(hi), None, None, "hermes", None
                )

    def _events(
        self, conn: sqlite3.Connection, profile: str, blob_store: BlobStore, stats: IngestStats
    ) -> Iterator[RawEvent]:
        have = table_columns(conn, "messages", self.denylist_tables)
        cols = [*_REQUIRED_TABLES["messages"], *[c for c in _OPTIONAL_MESSAGE_COLUMNS if c in have]]
        sql = f"SELECT {', '.join(cols)} FROM messages ORDER BY timestamp, id"
        assert_tables_allowed(sql, self.denylist_tables)
        sensitive_calls: dict[str, str] = {}
        for row in conn.execute(sql):
            stats.lines += 1
            ts = _ms(row["timestamp"])
            if row["session_id"] is None or ts is None:
                stats.skipped["malformed_record"] += 1
                continue
            stats.lines_by_type[str(row["role"])] += 1
            session_ref = _ns(profile, row["session_id"])
            uid = (
                row["message_uid"]
                if "message_uid" in row.keys() and row["message_uid"]
                else row["id"]
            )  # noqa: SIM118
            message_ref = f"{session_ref}:{uid}"
            role = row["role"] if row["role"] in ("user", "assistant") else None
            synthetic = role is None and row["role"] != "tool"
            if "_compressed_summary" in row.keys() and row["_compressed_summary"]:  # noqa: SIM118
                synthetic = True
            for block, kind, content, call_id, counts in self._blocks(row, sensitive_calls, stats):
                yield build_event(
                    source=SOURCE,
                    source_id=f"{message_ref}:{block}",
                    ts=ts,
                    session_ref=session_ref,
                    role=role if kind != "tool_result" else None,
                    kind=kind,
                    call_id=call_id,
                    synthetic=synthetic,
                    content=content,
                    hash_basis=f"{row['role']}:{block}:{content}",
                    message_ref=message_ref,
                    blob_store=blob_store,
                    stats=stats,
                    file_counts=counts,
                )

    def _blocks(
        self, row: sqlite3.Row, sensitive_calls: dict[str, str], stats: IngestStats
    ) -> Iterator[tuple[str, str, str, str | None, dict[str, int]]]:
        """Yield (block id, kind, text, call_id, file_redaction_counts)."""
        keys = row.keys()
        role = row["role"]
        content = row["content"] if isinstance(row["content"], str) else ""
        if role == "tool":
            call_id = row["tool_call_id"] if isinstance(row["tool_call_id"], str) else None
            if not content.strip():
                stats.skipped["empty_block"] += 1
                return
            counts: dict[str, int] = {}
            if call_id in sensitive_calls:
                content, counts = redact_file_text_counted(sensitive_calls[call_id], content)
            yield "result", "tool_result", content, call_id, counts
            return
        for name in ("reasoning_content", "reasoning"):
            text = row[name] if name in keys else None
            if isinstance(text, str) and text.strip():
                yield "reasoning", "reasoning", text, None, {}
                break
        if content.strip():
            yield "text", "text", content, None, {}
        calls = _json_list(row["tool_calls"])
        for i, call in enumerate(calls):
            if not isinstance(call, dict):
                stats.skipped["bad_tool_call"] += 1
                continue
            fn = call.get("function") if isinstance(call.get("function"), dict) else {}
            args_raw = fn.get("arguments")
            args: Any = args_raw
            if isinstance(args_raw, str):
                try:
                    args = json.loads(args_raw)
                except json.JSONDecodeError:
                    args = args_raw
            call_id = next(
                (call[k] for k in ("call_id", "id") if isinstance(call.get(k), str)), None
            )
            sensitive = sensitive_path_in(flatten_strings(args))
            if sensitive:
                for k in ("call_id", "id"):
                    if isinstance(call.get(k), str):
                        sensitive_calls[call[k]] = sensitive
            tool = str(fn.get("name") or "unknown")
            yield f"call{i}", f"tool:{tool}", tool_input_text(args), call_id, {}


def _json_list(value: Any) -> list[Any]:
    if not isinstance(value, str) or not value.strip():
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _ms(value: Any) -> int | None:
    """REAL epoch seconds (Hermes) -> epoch ms."""
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        return None
    return int(value * 1000)


def _opt(row: sqlite3.Row, key: str) -> str | None:
    if key not in row.keys():  # noqa: SIM118 - sqlite3.Row
        return None
    value = row[key]
    return value if isinstance(value, str) and value else None


if __name__ == "__main__":
    print_report(dry_run_report(HermesConnector()))
