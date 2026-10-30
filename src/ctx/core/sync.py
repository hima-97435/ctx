"""Source-agnostic sync engine.

Per enabled connector:
  permission re-check -> check_version (fail loud, before ANY write) -> fetch ->
  re-redact -> verify blob exists -> link project -> INSERT OR IGNORE event.

`events` is append-only: an existing (source, source_id) is never updated, so a
re-sync creates zero duplicates (rule 11). Sessions are mutable link metadata and
are upserted. The FTS index is maintained by triggers on `events` and is fully
rebuildable via `rebuild_fts`. The engine has no source-specific branches.
"""

import inspect
import json
import sqlite3
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ctx.connectors.base import Connector, ConnectorError, assert_path_allowed
from ctx.core import paths
from ctx.core.blobs import BlobStore
from ctx.core.episodes import rebuild_episodes
from ctx.core.model import RawEvent, RawSession
from ctx.core.projects import (
    ProjectConfig,
    link_project,
    load_roots,
    relink_sessions,
    sync_projects,
)
from ctx.core.redact import redact
from ctx.core.sync_state import SyncState
from ctx.core.tokens import estimate_tokens

BATCH_SIZE = 500


@dataclass
class SourceResult:
    source: str
    status: str = "ok"  # ok | skipped (disabled) | not_installed | error
    dry_run: bool = False
    schema_version: str | None = None
    sessions_seen: int = 0
    sessions_new: int = 0
    events_seen: int = 0
    events_new: int = 0
    events_duplicate: int = 0
    blobs: int = 0
    skipped_records: int = 0  # records the connector read but deliberately did not import
    unchanged_units: int = 0  # files/databases skipped by incremental state
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    detail: str | None = None  # e.g. the path reported missing for not_installed


def enforce_access(connector: Connector, source_path: Path) -> None:
    """Engine-side re-check of a connector's declared permissions (ADR-014).

    Raises ConnectorError / DenylistViolation unless `source_path` lies inside one
    of the connector's allowed paths and matches none of its path denylist.
    """
    if not connector.allowed_paths:
        raise ConnectorError(f"connector {connector.name!r} declares no allowed_paths")
    last: ConnectorError | None = None
    for root in connector.allowed_paths:
        try:
            assert_path_allowed(source_path, root, tuple(connector.denylist_paths))
            return
        except ConnectorError as exc:
            last = exc
            if type(exc) is not ConnectorError:  # DenylistViolation: stop, don't try others
                raise
    assert last is not None
    raise last


def ensure_source(conn: sqlite3.Connection, connector: Connector) -> sqlite3.Row:
    """Create/refresh the permission record. `enabled` is user state and is preserved."""
    now = datetime.now(UTC).isoformat(timespec="seconds")
    allowed = json.dumps([str(p) for p in connector.allowed_paths])
    conn.execute(
        "INSERT INTO sources(name, enabled, allowed_paths, created_at) VALUES (?, 1, ?, ?)"
        " ON CONFLICT(name) DO UPDATE SET allowed_paths=excluded.allowed_paths",
        (connector.name, allowed, now),
    )
    return conn.execute("SELECT * FROM sources WHERE name = ?", (connector.name,)).fetchone()


def sync(
    conn: sqlite3.Connection,
    connectors: Mapping[str, Connector],
    *,
    only: Iterable[str] | None = None,
    source_paths: Mapping[str, Path] | None = None,
    project_configs: list[ProjectConfig] | None = None,
    data_dir: Path | None = None,
    full: bool = False,
) -> list[SourceResult]:
    """Run every enabled (or `only` selected) connector. Errors are per-source.

    A source whose storage does not exist is `not_installed` (skipped) when picked
    implicitly, and an error when named in `only`. `full` ignores saved incremental
    state (every file/database is re-read; still zero duplicates).
    """
    data = Path(data_dir) if data_dir else paths.data_dir()
    names = list(only) if only else list(connectors)
    unknown = [n for n in names if n not in connectors]
    if unknown:
        raise ValueError(f"unknown source(s): {', '.join(unknown)}")
    if project_configs is not None:
        sync_projects(conn, project_configs)
        relink_sessions(conn)
    results = []
    for name in names:
        connector = connectors[name]
        row = ensure_source(conn, connector)
        if not row["enabled"]:
            results.append(SourceResult(name, status="skipped"))
            continue
        path = (source_paths or {}).get(name) or connector.default_source_path()
        try:
            absent = _absent(connector, path, explicit=bool(only))
            if absent is not None:
                results.append(absent)
                continue
            results.append(_sync_one(conn, connector, path, data, full=full))
        except (ConnectorError, OSError, sqlite3.Error) as exc:
            results.append(SourceResult(name, status="error", error=f"{type(exc).__name__}: {exc}"))
    rebuild_episodes(conn)  # derived from events; cheap and keeps the timeline current
    return results


def _absent(connector: Connector, path: Path, *, explicit: bool) -> SourceResult | None:
    """Permission-check, then report storage that does not exist (None when it does).

    The access check runs first so a denylisted path fails loud even if absent.
    """
    enforce_access(connector, path)
    if Path(path).exists():
        return None
    if explicit:
        return SourceResult(
            connector.name, status="error", error=f"source not installed: {path} does not exist"
        )
    return SourceResult(connector.name, status="not_installed", detail=str(path))


def _fetch(connector: Connector, path: Path, blob_store: BlobStore, tmp: Path, state: SyncState):
    kwargs = {"blob_store": blob_store, "tmp_dir": tmp}
    if "state" in inspect.signature(connector.fetch).parameters:
        kwargs["state"] = state
    return connector.fetch(path, **kwargs)


def _collect_stats(connector: Connector, result: SourceResult, state: SyncState) -> None:
    stats = getattr(connector, "last_stats", None)
    if stats is not None:
        result.skipped_records = sum(getattr(stats, "skipped", {}).values())
        result.warnings = list(getattr(stats, "warnings", []))
    result.unchanged_units = state.unchanged


def _sync_one(
    conn: sqlite3.Connection,
    connector: Connector,
    source_path: Path,
    data: Path,
    *,
    full: bool = False,
) -> SourceResult:
    enforce_access(connector, source_path)
    result = SourceResult(connector.name)
    result.schema_version = connector.check_version(source_path)  # raises before any write
    conn.execute(
        "UPDATE sources SET schema_version_seen = ? WHERE name = ?",
        (result.schema_version, connector.name),
    )
    blob_store = BlobStore(data)
    roots = load_roots(conn)
    known_sessions: set[str] = set()
    state = SyncState(conn, connector.name, use_saved=not full)
    pending = 0
    conn.execute("BEGIN")
    try:
        for record in _fetch(connector, source_path, blob_store, data / "tmp", state):
            if isinstance(record, RawSession):
                _upsert_session(conn, record, roots, known_sessions, result)
            else:
                _insert_event(conn, record, roots, known_sessions, blob_store, result)
            pending += 1
            if pending >= BATCH_SIZE:
                conn.execute("COMMIT")
                conn.execute("BEGIN")
                pending = 0
        state.flush(conn)  # cursors commit atomically with the events they cover
        conn.execute(
            "UPDATE sources SET last_sync = ? WHERE name = ?",
            (datetime.now(UTC).isoformat(timespec="seconds"), connector.name),
        )
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    _collect_stats(connector, result, state)
    return result


def _upsert_session(
    conn: sqlite3.Connection,
    s: RawSession,
    roots: list[tuple[str, str]],
    known: set[str],
    result: SourceResult,
) -> None:
    sid = f"{s.source}:{s.source_session_id}"
    existed = conn.execute("SELECT cwd FROM sessions WHERE id = ?", (sid,)).fetchone()
    # An incremental sync may see only part of a session; never erase what is known.
    cwd = s.cwd or (existed["cwd"] if existed else None)
    conn.execute(
        "INSERT INTO sessions(id, source, source_session_id, project_id, started_at, ended_at,"
        " cwd, model, agent, title) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(id) DO UPDATE SET project_id=excluded.project_id,"
        " ended_at=max(coalesce(sessions.ended_at, excluded.ended_at),"
        " coalesce(excluded.ended_at, sessions.ended_at)),"
        " cwd=coalesce(excluded.cwd, sessions.cwd), model=coalesce(excluded.model, sessions.model),"
        " agent=coalesce(excluded.agent, sessions.agent),"
        " title=coalesce(excluded.title, sessions.title)",
        (
            sid,
            s.source,
            s.source_session_id,
            link_project(cwd, roots),
            s.started_at,
            s.ended_at,
            cwd,
            s.model,
            s.agent,
            s.title,
        ),
    )
    known.add(sid)
    result.sessions_seen += 1
    if not existed:
        result.sessions_new += 1


def _insert_event(
    conn: sqlite3.Connection,
    ev: RawEvent,
    roots: list[tuple[str, str]],
    known: set[str],
    blob_store: BlobStore,
    result: SourceResult,
) -> None:
    result.events_seen += 1
    text = redact(ev.text)  # defense in depth: redaction is idempotent (ADR-012)
    token_estimate = ev.token_estimate if text == ev.text else estimate_tokens(text)
    if ev.blob_ref:
        if not blob_store.exists(ev.blob_ref):  # blob-before-event invariant
            raise ConnectorError(f"event {ev.source_id!r} references missing blob {ev.blob_ref}")
        result.blobs += 1
    session_id = f"{ev.source}:{ev.session_ref}" if ev.session_ref else None
    if session_id is not None and session_id not in known:
        # not yielded this run (incremental sync): still link if it is already stored
        if conn.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone():
            known.add(session_id)
        else:
            session_id = None
    project_id = None
    if session_id:
        row = conn.execute("SELECT project_id FROM sessions WHERE id = ?", (session_id,)).fetchone()
        project_id = row["project_id"] if row else None
    cur = conn.execute(
        "INSERT OR IGNORE INTO events(source, source_id, ts, project_id, session_id, role, kind,"
        " call_id, synthetic, text, token_estimate, truncated, blob_ref, raw_json, content_hash,"
        " message_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            ev.source,
            ev.source_id,
            ev.ts,
            project_id,
            session_id,
            ev.role,
            ev.kind,
            ev.call_id,
            int(ev.synthetic),
            text,
            token_estimate,
            int(ev.truncated),
            ev.blob_ref,
            ev.raw_json,
            ev.content_hash,
            ev.message_ref,
        ),
    )
    if cur.rowcount:
        result.events_new += 1
    else:
        result.events_duplicate += 1


def dry_run(
    connectors: Mapping[str, Connector],
    *,
    only: Iterable[str] | None = None,
    source_paths: Mapping[str, Path] | None = None,
    db_file: Path | None = None,
) -> list[SourceResult]:
    """Report what a sync would do. Writes nothing to the DB or the data dir
    (blobs and snapshots go to a throwaway temp dir)."""
    names = list(only) if only else list(connectors)
    unknown = [n for n in names if n not in connectors]
    if unknown:
        raise ValueError(f"unknown source(s): {', '.join(unknown)}")
    db_file = Path(db_file) if db_file else paths.db_path()
    existing = _open_existing(db_file)
    results = []
    try:
        for name in names:
            connector = connectors[name]
            path = (source_paths or {}).get(name) or connector.default_source_path()
            result = SourceResult(name, dry_run=True)
            try:
                absent = _absent(connector, path, explicit=bool(only))
                if absent is not None:
                    absent.dry_run = True
                    results.append(absent)
                    continue
                result.schema_version = connector.check_version(path)
                with tempfile.TemporaryDirectory(prefix="ctx-dry-run-") as tmp:
                    store = BlobStore(Path(tmp))
                    for record in connector.fetch(path, blob_store=store, tmp_dir=Path(tmp) / "t"):
                        _count(existing, record, result)
                _collect_stats(connector, result, SyncState())
            except (ConnectorError, OSError, sqlite3.Error) as exc:
                result.status = "error"
                result.error = f"{type(exc).__name__}: {exc}"
            results.append(result)
    finally:
        if existing is not None:
            existing.close()
    return results


def _open_existing(db_file: Path) -> sqlite3.Connection | None:
    if not db_file.exists():
        return None
    conn = sqlite3.connect(f"{db_file.as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _count(existing: sqlite3.Connection | None, record: RawSession | RawEvent, r: SourceResult):
    try:
        if isinstance(record, RawSession):
            r.sessions_seen += 1
            found = (
                existing is not None
                and existing.execute(
                    "SELECT 1 FROM sessions WHERE id = ?",
                    (f"{record.source}:{record.source_session_id}",),
                ).fetchone()
            )
            r.sessions_new += 0 if found else 1
        else:
            r.events_seen += 1
            found = (
                existing is not None
                and existing.execute(
                    "SELECT 1 FROM events WHERE source = ? AND source_id = ?",
                    (record.source, record.source_id),
                ).fetchone()
            )
            if found:
                r.events_duplicate += 1
            else:
                r.events_new += 1
            if record.blob_ref:
                r.blobs += 1
    except sqlite3.Error:  # DB exists but is not (yet) a ctx DB: treat everything as new
        if isinstance(record, RawSession):
            r.sessions_new += 1
        else:
            r.events_new += 1


def rebuild_fts(conn: sqlite3.Connection) -> int:
    """Rebuild the FTS index from `events` alone; returns the number of indexed events."""
    conn.execute("INSERT INTO events_fts(events_fts) VALUES ('rebuild')")
    return conn.execute("SELECT count(*) FROM events").fetchone()[0]


__all__ = [
    "SourceResult",
    "dry_run",
    "enforce_access",
    "ensure_source",
    "rebuild_fts",
    "sync",
]
