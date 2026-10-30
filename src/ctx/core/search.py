"""FTS5 search with filters and citations.

Visibility: only events whose project is `allowed=1` are returned unless
`include_all=True` (the `--all` flag), which bypasses allow-filtering but never
redaction (ARCHITECTURE invariant 2).
"""

import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ctx.core.redact import redact

_RELATIVE = re.compile(r"^(\d+)([smhdw])$")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}
_TERM = re.compile(r'"([^"]+)"|(\w+)')


@dataclass(frozen=True)
class Citation:
    source: str
    session_id: str | None  # sessions.id ('<source>:<source_session_id>')
    source_session_id: str | None
    message_id: str | None
    event_id: int
    source_id: str
    ts: int  # epoch ms
    project_id: str | None


@dataclass(frozen=True)
class SearchHit:
    citation: Citation
    kind: str
    role: str | None
    snippet: str
    rank: float
    token_estimate: int
    text: str | None = None  # full redacted event text, only when include_text=True


def parse_when(value: str, *, now: datetime | None = None, end_of_day: bool = False) -> int:
    """Parse '7d'/'12h'/'2w' (relative to now), 'YYYY-MM-DD', or an ISO datetime to epoch ms.

    Naive times are UTC. A bare date with end_of_day=True means the last ms of that day.
    """
    value = value.strip()
    now = now or datetime.now(UTC)
    m = _RELATIVE.match(value)
    if m:
        return int((now - timedelta(seconds=int(m[1]) * _UNITS[m[2]])).timestamp() * 1000)
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(
            f"cannot parse time {value!r}: use e.g. 7d, 12h, 2026-10-01 or 2026-10-01T09:30"
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    ms = int(dt.timestamp() * 1000)
    if end_of_day and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        ms += 86_400_000 - 1
    return ms


def fts_query(user_query: str) -> str:
    """Turn free text into a safe FTS5 query: every word/"phrase" is quoted, ANDed."""
    terms = []
    for m in _TERM.finditer(user_query):
        term = (m[1] or m[2]).replace('"', "")
        if term.strip():
            terms.append(f'"{term}"')
    if not terms:
        raise ValueError("empty search query")
    return " ".join(terms)


def resolve_project(conn: sqlite3.Connection, project: str) -> str:
    row = conn.execute(
        "SELECT id FROM projects WHERE id = ? OR name = ? COLLATE NOCASE", (project, project)
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown project {project!r}")
    return row["id"]


def search(
    conn: sqlite3.Connection,
    query: str,
    *,
    project: str | None = None,
    source: str | None = None,
    since: int | None = None,
    until: int | None = None,
    include_all: bool = False,
    limit: int = 20,
    include_text: bool = False,
) -> list[SearchHit]:
    """Ranked (bm25) matches with snippets and citations. `since`/`until` are epoch ms."""
    where = ["events_fts MATCH ?"]
    params: list[object] = [fts_query(query)]
    if project is not None:
        where.append("COALESCE(s.project_id, e.project_id) = ?")
        params.append(resolve_project(conn, project))
    if not include_all:
        where.append(
            "COALESCE(s.project_id, e.project_id) IN (SELECT id FROM projects WHERE allowed = 1)"
        )
    if source is not None:
        where.append("e.source = ?")
        params.append(source)
    if since is not None:
        where.append("e.ts >= ?")
        params.append(since)
    if until is not None:
        where.append("e.ts <= ?")
        params.append(until)
    sql = (
        "SELECT e.id, e.source, e.source_id, e.ts, e.kind, e.role, e.message_id, e.session_id,"
        " s.source_session_id, COALESCE(s.project_id, e.project_id) AS pid, e.token_estimate,"
        " snippet(events_fts, 0, '«', '»', '…', 24) AS snip, bm25(events_fts) AS rank,"
        " e.text AS full_text"
        " FROM events_fts JOIN events e ON e.id = events_fts.rowid"
        " LEFT JOIN sessions s ON s.id = e.session_id"
        f" WHERE {' AND '.join(where)} ORDER BY rank, e.ts, e.id LIMIT ?"
    )
    params.append(max(limit, 0))
    hits = []
    for r in conn.execute(sql, params):
        hits.append(
            SearchHit(
                citation=Citation(
                    source=r["source"],
                    session_id=r["session_id"],
                    source_session_id=r["source_session_id"],
                    message_id=r["message_id"],
                    event_id=r["id"],
                    source_id=r["source_id"],
                    ts=r["ts"],
                    project_id=r["pid"],
                ),
                kind=r["kind"],
                role=r["role"],
                snippet=redact(r["snip"]),
                rank=r["rank"],
                token_estimate=r["token_estimate"],
                text=redact(r["full_text"]) if include_text else None,
            )
        )
    return hits
