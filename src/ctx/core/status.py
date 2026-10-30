"""Read-only introspection for `ctx status` and `ctx doctor`.

Nothing here writes to the ctx database or to any source's storage. The database
is opened with `mode=ro`, so inspecting a missing/old/odd DB never creates or
migrates anything.
"""

import os
import sqlite3
import tempfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

from ctx.connectors.base import Connector
from ctx.core import paths
from ctx.core.db import MIGRATIONS


def open_readonly(db_file: Path) -> sqlite3.Connection | None:
    """Read-only connection, or None when the file does not exist."""
    if not Path(db_file).exists():
        return None
    conn = sqlite3.connect(f"{Path(db_file).resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _scalar(conn: sqlite3.Connection, sql: str) -> int | None:
    try:
        return conn.execute(sql).fetchone()[0]
    except sqlite3.Error:
        return None


def _group_counts(conn: sqlite3.Connection, table: str) -> dict[str, int]:
    try:
        rows = conn.execute(f"SELECT source, count(*) AS n FROM {table} GROUP BY source")
        return {r["source"]: r["n"] for r in rows}
    except sqlite3.Error:
        return {}


def collect_status(connectors: Mapping[str, Connector], db_file: Path) -> dict:
    """Per-source installed/path/last sync/counts plus DB-level facts."""
    conn = open_readonly(db_file)
    events: dict[str, int] = {}
    sessions: dict[str, int] = {}
    last_sync: dict[str, str | None] = {}
    enabled: dict[str, bool] = {}
    projects = None
    migrations: list[str] = []
    try:
        if conn is not None:
            events = _group_counts(conn, "events")
            sessions = _group_counts(conn, "sessions")
            projects = _scalar(conn, "SELECT count(*) FROM projects")
            try:
                for r in conn.execute("SELECT name, enabled, last_sync FROM sources"):
                    last_sync[r["name"]] = r["last_sync"]
                    enabled[r["name"]] = bool(r["enabled"])
                migrations = [r[0] for r in conn.execute("SELECT name FROM schema_migrations")]
            except sqlite3.Error:
                pass
    finally:
        if conn is not None:
            conn.close()
    sources = []
    for name, connector in connectors.items():
        path = connector.default_source_path()
        sources.append(
            {
                "name": name,
                "installed": path.exists(),
                "path": str(path),
                "enabled": enabled.get(name, True),
                "last_sync": last_sync.get(name),
                "sessions": sessions.get(name, 0),
                "events": events.get(name, 0),
            }
        )
    return {
        "db_path": str(db_file),
        "db_exists": conn is not None,
        "data_dir": str(paths.data_dir()),
        "projects": projects or 0,
        "events_total": sum(events.values()),
        "migrations_applied": len(migrations),
        "migrations_known": len(MIGRATIONS),
        "sources": sources,
    }


@dataclass
class Check:
    name: str
    status: str  # ok | warn | fail
    detail: str
    fix: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def _check_fts5() -> Check:
    try:
        mem = sqlite3.connect(":memory:")
        mem.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        mem.close()
    except sqlite3.Error as exc:
        return Check(
            "fts5",
            "fail",
            f"this Python's SQLite ({sqlite3.sqlite_version}) lacks FTS5: {exc}",
            "use a Python build linked against SQLite with FTS5 (e.g. `uv python install 3.12`)",
        )
    return Check("fts5", "ok", f"FTS5 available (SQLite {sqlite3.sqlite_version})")


def _check_writable(data_dir: Path) -> Check:
    if data_dir.is_dir():
        try:
            with tempfile.NamedTemporaryFile(dir=data_dir, prefix=".ctx-doctor-"):
                pass
        except OSError as exc:
            return Check(
                "data_dir",
                "fail",
                f"{data_dir} is not writable: {exc}",
                f"fix permissions on {data_dir} or set CTX_HOME to a writable directory",
            )
        return Check("data_dir", "ok", f"{data_dir} is writable")
    existing = data_dir
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    if existing.is_dir() and os.access(existing, os.W_OK):
        return Check("data_dir", "ok", f"{data_dir} does not exist yet; it can be created")
    return Check(
        "data_dir",
        "fail",
        f"{data_dir} does not exist and {existing} is not writable",
        "set CTX_HOME to a writable directory",
    )


def _check_schema(conn: sqlite3.Connection) -> Check:
    try:
        applied = {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}
    except sqlite3.Error:
        return Check(
            "schema", "fail", "no schema_migrations table; not a ctx database", "run `ctx sync`"
        )
    known = [name for name, _ in MIGRATIONS]
    unknown = sorted(applied - set(known))
    pending = [n for n in known if n not in applied]
    if unknown:
        return Check(
            "schema",
            "fail",
            f"database has migrations this ctx does not know: {', '.join(unknown)}",
            "upgrade ctx (`git pull && uv sync`); do not run an older ctx on this database",
        )
    if pending:
        return Check(
            "schema",
            "warn",
            f"{len(applied)}/{len(known)} migrations applied; pending: {', '.join(pending)}",
            "run any ctx command that opens the database (e.g. `ctx sync`) to migrate",
        )
    return Check("schema", "ok", f"schema up to date ({known[-1]}, {len(known)} migrations)")


def _check_fts_integrity(conn: sqlite3.Connection) -> Check:
    fix = "run `ctx reindex` (rebuilds the index from events alone)"
    events = _scalar(conn, "SELECT count(*) FROM events")
    indexed = _scalar(conn, "SELECT count(*) FROM events_fts_docsize")
    if events is None or indexed is None:
        return Check("fts_integrity", "fail", "events or events_fts table is missing", fix)
    missing = _scalar(
        conn, "SELECT count(*) FROM events WHERE id NOT IN (SELECT id FROM events_fts_docsize)"
    )
    if events != indexed or missing:
        return Check(
            "fts_integrity",
            "fail",
            f"{events} events but {indexed} indexed rows ({missing} events not indexed)",
            fix,
        )
    return Check("fts_integrity", "ok", f"{events} events, {indexed} indexed rows")


def _check_db_integrity(conn: sqlite3.Connection) -> Check:
    try:
        result = conn.execute("PRAGMA quick_check").fetchone()[0]
    except sqlite3.Error as exc:
        return Check("db_integrity", "fail", f"quick_check failed: {exc}", _RESTORE)
    if result != "ok":
        return Check("db_integrity", "fail", f"quick_check: {result}", _RESTORE)
    return Check("db_integrity", "ok", "SQLite quick_check passed")


_RESTORE = (
    "restore ctx.db from a backup; events are rebuildable from the source tools via `ctx sync`"
)


def run_doctor(db_file: Path, data_dir: Path | None = None) -> list[Check]:
    """Run every check; never writes to the database."""
    data = Path(data_dir) if data_dir else paths.data_dir()
    checks = [_check_fts5(), _check_writable(data)]
    try:
        conn = open_readonly(db_file)
    except sqlite3.Error as exc:
        checks.append(Check("database", "fail", f"cannot open {db_file}: {exc}", _RESTORE))
        return checks
    if conn is None:
        checks.append(
            Check(
                "database", "warn", f"no database at {db_file} yet", "run `ctx sync` to create it"
            )
        )
        return checks
    try:
        checks.append(Check("database", "ok", f"{db_file} opens read-only"))
        checks.append(_check_db_integrity(conn))
        schema = _check_schema(conn)
        checks.append(schema)
        if schema.status != "fail":
            checks.append(_check_fts_integrity(conn))
    finally:
        conn.close()
    return checks
