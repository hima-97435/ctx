"""Source-neutral helpers for connectors that read a live SQLite database.

Nothing here knows a specific tool's schema (ADR-007). The one non-obvious part is
opening a WAL-mode database read-only without ever writing to the source:

1. `mode=ro` is tried first. It sees uncheckpointed `-wal` content, so it is the
   only mode that is correct while the tool is running.
2. A WAL database whose `-shm`/`-wal` files are absent cannot be opened `mode=ro`
   (SQLite would have to create them) and fails with CANTOPEN. If there is no
   non-empty `-wal` next to it there is nothing to miss, so `immutable=1` is safe.
   If a non-empty `-wal` exists, we raise instead: `immutable=1` would silently
   drop its rows.
"""

import sqlite3
from pathlib import Path

from ctx.connectors.base import ConnectorError, assert_tables_allowed


def _wal_has_content(path: Path) -> bool:
    wal = path.with_name(path.name + "-wal")
    try:
        return wal.stat().st_size > 0
    except OSError:
        return False


def connect_readonly(path: Path) -> sqlite3.Connection:
    """Open `path` read-only (see module docstring); never writes anywhere."""
    if not path.exists():
        raise ConnectorError(f"SQLite database not found: {path}")
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except sqlite3.OperationalError as exc:
        if _wal_has_content(path):
            raise ConnectorError(
                f"cannot open {path.name} read-only while it has uncheckpointed WAL content: {exc}"
            ) from exc
        try:
            conn = sqlite3.connect(f"file:{path}?immutable=1", uri=True)
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        except sqlite3.Error as exc2:
            raise ConnectorError(f"cannot open {path.name} read-only: {exc2}") from exc2
    conn.row_factory = sqlite3.Row
    return conn


def open_snapshot(path: Path, tmp_dir: Path, name: str) -> sqlite3.Connection:
    """Copy the live DB with the SQLite backup API and open the copy (rows only)."""
    tmp = Path(tmp_dir)
    tmp.mkdir(parents=True, exist_ok=True)
    snapshot_path = tmp / name
    snapshot_path.unlink(missing_ok=True)
    src = connect_readonly(path)
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


def table_columns(
    conn: sqlite3.Connection, table: str, denylist_tables: frozenset[str]
) -> dict[str, str]:
    """{column: declared type} for a table we are allowed to read ({} if missing)."""
    assert_tables_allowed(f"table {table}", denylist_tables)
    return {row["name"]: row["type"] for row in conn.execute(f'PRAGMA table_info("{table}")')}


def missing_columns(
    conn: sqlite3.Connection,
    required: dict[str, set[str]],
    denylist_tables: frozenset[str],
) -> list[str]:
    """Human-readable structural problems against a {table: required columns} pin."""
    problems: list[str] = []
    for table, cols in sorted(required.items()):
        existing = table_columns(conn, table, denylist_tables)
        if not existing:
            problems.append(f"missing table {table}")
        elif cols - existing.keys():
            problems.append(f"table {table} missing columns {sorted(cols - existing.keys())}")
    return problems
