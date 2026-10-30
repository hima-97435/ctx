"""OpenCode connector (source name: "opencode").

Reads ~/.local/share/opencode/opencode.db (SQLite, live WAL — see
docs/CONNECTORS.md for the observed schema and quirks). All OpenCode-specific
logic lives here and only here (ADR-007).

Enforced on every call, before any reads or writes:
- structural schema pin: fails loudly with ConnectorError when the DB layout
  doesn't match what this connector knows (ADR-011)
- hard denylists: secret-bearing tables are never queried; files matching the
  path denylist are never opened (ADR-014)
- size-aware extraction: rows are streamed one at a time; oversized extracted
  content is redacted first and then spilled to a blob (redact-then-spill,
  ADR-012 + ADR-017)

Project resolution is NOT done here: iter_events yields events with cwd-less
sessions; the sync engine links projects via cwd roots (ADR-009/ADR-010).

Run `python -m ctx.connectors.opencode` for a dry-run report over the real data:
a read-only pass printing redaction match counts by kind. It writes nothing.
"""

import hashlib
import json
import sqlite3
import time
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from ctx.connectors.base import (
    ConnectorError,
    assert_path_allowed,
    assert_tables_allowed,
)
from ctx.core.blobs import BlobStore
from ctx.core.model import RawEvent, RawSession
from ctx.core.paths import data_dir
from ctx.core.redact import (
    is_sensitive_file,
    redact_counted,
    redact_file_text_counted,
)
from ctx.core.sync_state import SyncState, fingerprint
from ctx.core.truncate import SPILL_THRESHOLD_CHARS, prepare_text

SOURCE = "opencode"
SOURCE_ROOT = Path.home() / ".local" / "share" / "opencode"
DB_PATH = SOURCE_ROOT / "opencode.db"
ALLOWED_PATHS = (SOURCE_ROOT,)
DENYLIST_PATHS = ("auth.json",)
# Secret-bearing and internal tables this connector must never touch
# (docs/CONNECTORS.md); enforced via assert_tables_allowed on every statement.
DENYLIST_TABLES = frozenset(
    {
        "account",
        "account_state",
        "control_account",
        "credential",
        "event",
        "session_context_epoch",
        "session_share",
    }
)

# Structural pin: these tables with these columns must exist (ADR-011).
_REQUIRED_TABLES: dict[str, set[str]] = {
    "__drizzle_migrations": {"id", "hash"},
    "message": {"data", "id", "session_id", "time_created", "time_updated"},
    "part": {"data", "id", "message_id", "session_id", "time_created", "time_updated"},
    "session": {
        "agent",
        "directory",
        "id",
        "model",
        "project_id",
        "time_created",
        "time_updated",
        "title",
        "version",
    },
}

_SKIPPED_PART_TYPES = frozenset({"step-start", "step-finish"})


def check_version(conn: sqlite3.Connection) -> str:
    """Verify the DB matches the structural pin; return a version marker.

    Raises ConnectorError (before anything is written) on unrecognized schema.
    """
    problems: list[str] = []
    for table, required in sorted(_REQUIRED_TABLES.items()):
        assert_tables_allowed(f"table {table}", DENYLIST_TABLES)
        existing = {row["name"] for row in conn.execute(f'PRAGMA table_info("{table}")')}
        if not existing:
            problems.append(f"missing table {table}")
            continue
        missing = required - existing
        if missing:
            problems.append(f"table {table} missing columns {sorted(missing)}")
    if problems:
        raise ConnectorError(
            "OpenCode schema unrecognized; failing loud, nothing was written: "
            + "; ".join(problems)
        )
    row = conn.execute(
        "SELECT hash, name FROM __drizzle_migrations ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return "drizzle:unknown"
    return f"drizzle:{(row['hash'] or row['name'] or '')[:12]}"


def open_readonly(
    db_path: Path | None = None,
    *,
    source_root: Path = SOURCE_ROOT,
    denylist_paths: tuple[str, ...] = DENYLIST_PATHS,
) -> sqlite3.Connection:
    """Open the OpenCode DB read-only (no snapshot, no writes anywhere)."""
    path = Path(db_path) if db_path else DB_PATH
    assert_path_allowed(path, source_root, denylist_paths)
    if not path.exists():
        raise ConnectorError(f"OpenCode DB not found: {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def open_snapshot(
    db_path: Path | None = None,
    *,
    tmp_dir: Path | None = None,
    source_root: Path = SOURCE_ROOT,
    denylist_paths: tuple[str, ...] = DENYLIST_PATHS,
) -> sqlite3.Connection:
    """Copy the live WAL DB via the SQLite backup API and open the copy.

    Used by real sync runs so a concurrently-running OpenCode is never blocked
    and we never read a torn state. `data/tmp/` lives inside the ctx data dir.
    """
    path = Path(db_path) if db_path else DB_PATH
    assert_path_allowed(path, source_root, denylist_paths)
    tmp = Path(tmp_dir) if tmp_dir else data_dir() / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    snapshot_path = tmp / "opencode-snapshot.db"
    if snapshot_path.exists():
        snapshot_path.unlink()
    src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(snapshot_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    snap = sqlite3.connect(snapshot_path)
    snap.row_factory = sqlite3.Row
    return snap


@dataclass(frozen=True)
class _Extracted:
    part_type: str
    kind: str
    content: str
    call_id: str | None
    synthetic: bool
    file_counts: dict[str, int] = field(default_factory=dict)


def _tool_content(state: dict) -> tuple[str, dict[str, int]]:
    """Tool content = input JSON + output; sensitive-file outputs redacted.

    Returns (content, counts of file-aware redactions) so the dry-run report
    can include them.
    """
    raw_input = state.get("input")
    tool_input = raw_input if isinstance(raw_input, dict) else {}
    output = state.get("output") or ""
    input_str = json.dumps(tool_input, ensure_ascii=False, sort_keys=True)
    if output:
        for value in tool_input.values():
            if isinstance(value, str) and is_sensitive_file(value):
                content, counts = redact_file_text_counted(value, output)
                return f"input: {input_str}\n\n{content}", counts
        return f"input: {input_str}\n\n{output}", {}
    return f"input: {input_str}", {}


def _extract_part(data: dict) -> _Extracted | None:
    """Map one OpenCode part JSON to (part_type, kind, content, call_id, synthetic).

    Returns None for content-less step markers. Mapping documented in
    docs/CONNECTORS.md.
    """
    part_type = str(data.get("type") or "unknown")
    if part_type in _SKIPPED_PART_TYPES:
        return None
    call_id = data.get("callID")
    synthetic = bool(data.get("synthetic"))

    if part_type == "text":
        return _Extracted(part_type, "text", data.get("text") or "", call_id, synthetic)
    if part_type == "reasoning":
        return _Extracted(part_type, "reasoning", data.get("text") or "", call_id, synthetic)
    if part_type == "tool":
        tool = str(data.get("tool") or "unknown")
        state = data.get("state") or {}
        content, file_counts = _tool_content(state)
        return _Extracted(part_type, f"tool:{tool}", content, call_id, synthetic, file_counts)
    if part_type == "patch":
        files = ", ".join(data.get("files") or [])
        content = f"patch {data.get('hash') or ''}: {files}".strip()
        return _Extracted(part_type, "patch", content, call_id, synthetic)
    if part_type == "file":
        content = (
            f"{data.get('filename') or ''} ({data.get('mime') or ''}) {data.get('url') or ''}"
        ).strip()
        return _Extracted(part_type, "file", content, call_id, synthetic)
    if part_type == "compaction":
        content = (
            f"compaction marker: auto={data.get('auto')}, tail_start_id={data.get('tail_start_id')}"
        )
        return _Extracted(part_type, "compaction", content, call_id, True)
    # Unknown part type: keep the JSON so nothing is silently dropped.
    content = json.dumps(data, ensure_ascii=False, sort_keys=True)
    return _Extracted(part_type, part_type, content, call_id, synthetic)


def _message_roles(conn: sqlite3.Connection) -> dict[str, str | None]:
    sql = "SELECT id, json_extract(data, '$.role') AS role FROM message"
    assert_tables_allowed(sql, DENYLIST_TABLES)
    return {row["id"]: row["role"] for row in conn.execute(sql)}


def iter_sessions(conn: sqlite3.Connection) -> Iterator[RawSession]:
    sql = (
        "SELECT id, directory, title, agent, model, time_created, time_updated "
        "FROM session ORDER BY time_created, id"
    )
    assert_tables_allowed(sql, DENYLIST_TABLES)
    for row in conn.execute(sql):
        model = row["model"]
        if model and model.startswith("{"):
            try:
                parsed = json.loads(model)
                model = f"{parsed.get('providerID', '')}/{parsed.get('id', '')}".strip("/")
            except json.JSONDecodeError:
                pass
        yield RawSession(
            source=SOURCE,
            source_session_id=row["id"],
            started_at=row["time_created"],
            ended_at=row["time_updated"],
            cwd=row["directory"],
            model=model,
            agent=row["agent"],
            title=row["title"],
        )


def iter_events(conn: sqlite3.Connection, *, blob_store: BlobStore) -> Iterator[RawEvent]:
    """Stream parts as redacted, truncated RawEvents (rows read one at a time)."""
    roles = _message_roles(conn)
    sql = (
        "SELECT id, message_id, session_id, time_created, data FROM part ORDER BY time_created, id"
    )
    assert_tables_allowed(sql, DENYLIST_TABLES)
    for row in conn.execute(sql):
        data_str = row["data"]
        try:
            data = json.loads(data_str)
        except json.JSONDecodeError:
            continue  # corrupt row: never crash a sync on one bad part
        extracted = _extract_part(data)
        if extracted is None:
            continue
        redacted, _counts = redact_counted(extracted.content)
        prepared = prepare_text(redacted, blob_store=blob_store)
        yield RawEvent(
            source=SOURCE,
            source_id=row["id"],
            ts=row["time_created"],
            session_ref=row["session_id"],
            role=roles.get(row["message_id"]),
            kind=extracted.kind,
            call_id=extracted.call_id,
            synthetic=extracted.synthetic,
            text=prepared.text,
            token_estimate=prepared.token_estimate,
            truncated=prepared.truncated,
            blob_ref=prepared.blob_ref,
            raw_json=None,
            content_hash=hashlib.sha256(data_str.encode("utf-8")).hexdigest(),
            message_ref=row["message_id"],
        )


SCHEMA_VERSION_PIN = "opencode-structural-v1"


class OpenCodeConnector:
    """Connector-protocol adapter over this module's functions (see base.Connector)."""

    name = SOURCE
    schema_version_pin = SCHEMA_VERSION_PIN
    denylist_paths = DENYLIST_PATHS
    denylist_tables = DENYLIST_TABLES

    def __init__(self, source_root: Path | None = None) -> None:
        self.source_root = Path(source_root) if source_root else SOURCE_ROOT
        self.allowed_paths = [self.source_root]

    def default_source_path(self) -> Path:
        return self.source_root / "opencode.db"

    def check_version(self, source_path: Path) -> str:
        conn = open_readonly(
            source_path, source_root=self.source_root, denylist_paths=self.denylist_paths
        )
        try:
            return check_version(conn)
        finally:
            conn.close()

    def fetch(
        self,
        source_path: Path,
        *,
        blob_store: BlobStore,
        tmp_dir: Path,
        state: SyncState | None = None,
    ) -> Iterator[RawSession | RawEvent]:
        self.check_version(source_path)  # fail loud before reading content or yielding
        state = state or SyncState()
        db = Path(source_path)
        print_ = fingerprint(db, db.with_name(db.name + "-wal"))  # before reading (ADR-024)
        if state.is_unchanged(f"db:{db}", print_):
            return
        conn = open_snapshot(
            source_path,
            tmp_dir=tmp_dir,
            source_root=self.source_root,
            denylist_paths=self.denylist_paths,
        )
        try:
            yield from iter_sessions(conn)
            yield from iter_events(conn, blob_store=blob_store)
            state.stage(f"db:{db}", print_)
        finally:
            conn.close()
            (Path(tmp_dir) / "opencode-snapshot.db").unlink(missing_ok=True)


@dataclass
class DryRunReport:
    """What a real sync would ingest, without writing anything."""

    db_path: str
    schema_version: str
    sessions: int = 0
    messages: int = 0
    parts: int = 0
    skipped_parts: int = 0
    unparsable_parts: int = 0
    elapsed_s: float = 0.0
    roles: Counter = field(default_factory=Counter)
    parts_by_type: Counter = field(default_factory=Counter)
    parts_by_tool: Counter = field(default_factory=Counter)
    redaction_matches_by_kind: Counter = field(default_factory=Counter)
    parts_with_redaction_by_type: Counter = field(default_factory=Counter)
    oversized_parts: int = 0
    oversized_content_chars: int = 0
    tables_queried: tuple[str, ...] = ("session", "message", "part")
    denylisted_tables: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.denylisted_tables:
            self.denylisted_tables = tuple(sorted(DENYLIST_TABLES))


def dry_run_report(
    db_path: Path | None = None,
    *,
    source_root: Path = SOURCE_ROOT,
    denylist_paths: tuple[str, ...] = DENYLIST_PATHS,
) -> DryRunReport:
    """Read-only pass over the real OpenCode DB; writes nothing anywhere."""
    path = Path(db_path) if db_path else DB_PATH
    start = time.perf_counter()
    conn = open_readonly(path, source_root=source_root, denylist_paths=denylist_paths)
    try:
        report = DryRunReport(db_path=str(path), schema_version=check_version(conn))
        report.sessions = conn.execute("SELECT count(*) FROM session").fetchone()[0]
        report.messages = conn.execute("SELECT count(*) FROM message").fetchone()[0]
        roles = _message_roles(conn)
        sql = "SELECT id, message_id, data FROM part"
        assert_tables_allowed(sql, DENYLIST_TABLES)
        for row in conn.execute(sql):
            report.parts += 1
            try:
                data = json.loads(row["data"])
            except json.JSONDecodeError:
                report.unparsable_parts += 1
                continue
            extracted = _extract_part(data)
            if extracted is None:
                report.skipped_parts += 1
                continue
            report.parts_by_type[extracted.part_type] += 1
            if extracted.part_type == "tool":
                report.parts_by_tool[extracted.kind.removeprefix("tool:")] += 1
            role = roles.get(row["message_id"])
            if role:
                report.roles[role] += 1
            redacted, counts = redact_counted(extracted.content)
            if extracted.file_counts:
                for kind, n in extracted.file_counts.items():
                    counts[kind] = counts.get(kind, 0) + n
            if counts:
                for kind, n in counts.items():
                    report.redaction_matches_by_kind[kind] += n
                report.parts_with_redaction_by_type[extracted.part_type] += 1
            if len(extracted.content) > SPILL_THRESHOLD_CHARS:
                report.oversized_parts += 1
                report.oversized_content_chars += len(extracted.content)
        report.elapsed_s = time.perf_counter() - start
        return report
    finally:
        conn.close()


def print_report(report: DryRunReport) -> None:
    print("OpenCode connector — dry-run report (read-only, nothing written)")
    print(f"  db:              {report.db_path}")
    print(f"  schema_version:  {report.schema_version}")
    print(
        f"  sessions {report.sessions} · messages {report.messages} · parts {report.parts}"
        f" · skipped {report.skipped_parts} · unparsable {report.unparsable_parts}"
    )
    print(f"  elapsed:         {report.elapsed_s:.2f}s")
    print(f"  roles:           {dict(report.roles)}")
    print(f"  parts by type:   {dict(report.parts_by_type)}")
    if report.parts_by_tool:
        print(f"  parts by tool:   {dict(report.parts_by_tool)}")
    print("  redaction matches by kind:")
    for kind, n in sorted(report.redaction_matches_by_kind.items(), key=lambda x: -x[1]):
        print(f"    {kind:24s} {n}")
    if not report.redaction_matches_by_kind:
        print("    (none)")
    print(f"  parts with redaction, by type: {dict(report.parts_with_redaction_by_type)}")
    print(
        f"  oversized (>{SPILL_THRESHOLD_CHARS} chars): {report.oversized_parts} parts,"
        f" {report.oversized_content_chars} chars would be spilled to blobs"
    )
    print(f"  tables queried:  {', '.join(report.tables_queried)}")
    print(f"  denylisted (never queried): {', '.join(report.denylisted_tables)}")


if __name__ == "__main__":
    print_report(dry_run_report())
