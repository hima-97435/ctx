"""Read-only MCP server over the ctx store (stdio). Source-agnostic (AGENTS rule 6).

Every tool is a thin wrapper over core functions; context assembly goes only through
`build_context` (rule 8). Visibility is fixed here: only `allowed=1` projects that are
not `mcp_exclude=1` are ever returned (ADR-016, ADR-022). There is no way to widen it
from a tool argument. All returned text is re-redacted (rule 4) and size-capped.

The one non-read tool, `sync`, runs the idempotent importer: it writes only ctx's own
database/blobs and reads source tools' storage read-only, exactly like `ctx sync`.
"""

import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime

from mcp.server.mcpserver import MCPServer

from ctx.connectors.registry import available_connectors
from ctx.core import paths
from ctx.core.context import build_context
from ctx.core.db import connect
from ctx.core.episodes import (
    build_timeline,
    local_day_bounds,
    parse_day,
    parse_local,
    render_timeline,
    resolve_tz,
)
from ctx.core.handoff import TARGETS as HANDOFF_TARGETS
from ctx.core.handoff import collect as collect_handoff
from ctx.core.handoff import render_handoff
from ctx.core.projects import load_projects_config
from ctx.core.redact import redact
from ctx.core.retrieve import retrieve
from ctx.core.search import parse_when, resolve_project
from ctx.core.sync import sync as run_sync
from ctx.core.truncate import truncate_to_tokens

MAX_RESPONSE_TOKENS = 8000  # hard cap on any single tool response
DEFAULT_BUDGET_TOKENS = 2000
MAX_LIMIT = 50
MAX_EVENT_TOKENS = 800  # per-message cap inside get_session

VISIBLE = "allowed = 1 AND mcp_exclude = 0"
VISIBLE_P = "p.allowed = 1 AND p.mcp_exclude = 0"

INSTRUCTIONS = (
    "ctx is the user's local memory of past coding-agent sessions. Use search to find "
    "past discussions, get_context for a budgeted cited bundle, get_session to read a "
    "conversation around a cited message, timeline for what was worked on when. Results "
    "are redacted and cite source/session/message ids."
)


def _clamp_limit(limit: int) -> int:
    if limit < 1:
        raise ValueError("limit must be >= 1")
    return min(limit, MAX_LIMIT)


def _finish(text: str) -> str:
    """Final safety net for every response: re-redact, then cap to MAX_RESPONSE_TOKENS."""
    return truncate_to_tokens(redact(text), MAX_RESPONSE_TOKENS)


def _fmt_ts(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d %H:%M")


def _visible_project(conn: sqlite3.Connection, project: str | None) -> str | None:
    """Resolve a project id/name; excluded or disallowed projects look unknown."""
    if project is None:
        return None
    pid = resolve_project(conn, project)
    row = conn.execute(f"SELECT 1 FROM projects WHERE id = ? AND {VISIBLE}", (pid,)).fetchone()
    if row is None:
        raise ValueError(f"unknown project {project!r}")
    return pid


def _check_source(source: str | None) -> None:
    known = available_connectors()
    if source is not None and source not in known:
        raise ValueError(f"unknown source {source!r}; known: {', '.join(known)}")


def _cite(source: str, session: str | None, message: str | None, event: int, ts: int) -> str:
    return f"{source} session={session or '-'} message={message or '-'} event={event} {_fmt_ts(ts)}"


def tool_search(
    conn: sqlite3.Connection,
    query: str,
    project: str | None = None,
    source: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 10,
) -> str:
    _check_source(source)
    hits = retrieve(
        conn,
        query,
        project=_visible_project(conn, project),
        source=source,
        since=parse_when(since) if since else None,
        until=parse_when(until, end_of_day=True) if until else None,
        exclude_mcp=True,
        limit=_clamp_limit(limit),
    )
    if not hits:
        return "no results"
    lines = []
    for n, h in enumerate(hits, 1):
        c = h.citation
        ref = _cite(c.source, c.source_session_id, c.message_id, c.event_id, c.ts)
        lines.append(f"[{n}] ({ref} project={c.project_id or '-'} {h.kind})\n{h.snippet}")
    return "\n\n".join(lines)


def tool_get_context(
    conn: sqlite3.Connection,
    query: str,
    project: str | None = None,
    budget_tokens: int = DEFAULT_BUDGET_TOKENS,
) -> str:
    if budget_tokens < 1:
        raise ValueError("budget_tokens must be >= 1")
    budget = min(budget_tokens, MAX_RESPONSE_TOKENS)
    package = build_context(conn, query, budget, _visible_project(conn, project), exclude_mcp=True)
    if not package.items:
        return "no results"
    left = ", ".join(f"event={x.event_id}" for x in package.left_out[:20])
    footer = f"-- {len(package.items)} items, {package.tokens_used}/{budget} tokens"
    if package.left_out:
        footer += f"; {len(package.left_out)} more matches left out (over budget): {left}"
    return f"{package.render()}\n\n{footer}"


def tool_get_session(
    conn: sqlite3.Connection,
    source: str,
    session_id: str,
    around_message_id: str | None = None,
    limit: int = 20,
) -> str:
    limit = _clamp_limit(limit)
    sess = conn.execute(
        "SELECT s.id, s.title, s.started_at, s.project_id FROM sessions s"
        f" JOIN projects p ON p.id = s.project_id AND {VISIBLE_P}"
        " WHERE s.source = ? AND s.source_session_id = ?",
        (source, session_id),
    ).fetchone()
    if sess is None:
        raise ValueError(f"no visible session {source}/{session_id}")
    rows = conn.execute(
        "SELECT id, ts, role, kind, message_id, text FROM events WHERE session_id = ?"
        " ORDER BY ts, id",
        (sess["id"],),
    ).fetchall()
    start = 0
    if around_message_id is not None:
        idx = next((i for i, r in enumerate(rows) if r["message_id"] == around_message_id), None)
        if idx is None:
            raise ValueError(f"message {around_message_id!r} not found in this session")
        start = max(0, min(idx - limit // 2, len(rows) - limit))
    window = rows[start : start + limit]
    head = (
        f"session {source}/{session_id} project={sess['project_id']} "
        f"title={sess['title'] or '-'} started={_fmt_ts(sess['started_at'])} "
        f"messages {start + 1}-{start + len(window)} of {len(rows)}"
    )
    out = [head]
    for r in window:
        text = truncate_to_tokens(redact(r["text"]), MAX_EVENT_TOKENS)
        ref = _cite(source, session_id, r["message_id"], r["id"], r["ts"])
        out.append(f"({ref} {r['role'] or '-'} {r['kind']})\n{text}")
    return "\n\n".join(out)


def tool_list_projects(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        "SELECT p.id, p.name, COUNT(s.id) AS n, MAX(s.started_at) AS last"
        " FROM projects p LEFT JOIN sessions s ON s.project_id = p.id"
        f" WHERE {VISIBLE_P} GROUP BY p.id ORDER BY p.id"
    ).fetchall()
    if not rows:
        return "no projects"
    return "\n".join(
        f"{r['id']} | {r['name']} | sessions={r['n']} | "
        f"last={_fmt_ts(r['last']) if r['last'] else '-'}"
        for r in rows
    )


def tool_recent_sessions(
    conn: sqlite3.Connection,
    project: str | None = None,
    source: str | None = None,
    limit: int = 10,
) -> str:
    _check_source(source)
    pid = _visible_project(conn, project)
    where = [f"{VISIBLE_P}"]
    params: list[object] = []
    if pid is not None:
        where.append("s.project_id = ?")
        params.append(pid)
    if source is not None:
        where.append("s.source = ?")
        params.append(source)
    params.append(_clamp_limit(limit))
    rows = conn.execute(
        "SELECT s.source, s.source_session_id, s.title, s.started_at, s.project_id,"
        " (SELECT COUNT(*) FROM events e WHERE e.session_id = s.id) AS n"
        " FROM sessions s JOIN projects p ON p.id = s.project_id"
        f" WHERE {' AND '.join(where)} ORDER BY s.started_at DESC, s.id LIMIT ?",
        params,
    ).fetchall()
    if not rows:
        return "no sessions"
    return "\n".join(
        f"{r['source']} session={r['source_session_id']} {_fmt_ts(r['started_at'])} "
        f"project={r['project_id']} events={r['n']} title={r['title'] or '-'}"
        for r in rows
    )


def tool_timeline(
    conn: sqlite3.Connection,
    project: str | None = None,
    source: str | None = None,
    since: str | None = None,
    until: str | None = None,
    day: str | None = None,
    tz: str | None = None,
    limit: int = 30,
) -> str:
    _check_source(source)
    zone = resolve_tz(tz)
    if day and (since or until):
        raise ValueError("day cannot be combined with since/until")
    if day:
        lo, hi = local_day_bounds(parse_day(day, zone), zone)
    else:
        lo = parse_local(since, zone) if since else None
        hi = parse_local(until, zone, end_of_day=True) if until else None
    entries = build_timeline(
        conn,
        zone,
        project=_visible_project(conn, project),
        source=source,
        since=lo,
        until=hi,
        exclude_mcp=True,
        limit=_clamp_limit(limit),
    )
    return render_timeline(entries, zone)


def tool_handoff(
    conn: sqlite3.Connection,
    project: str | None = None,
    since: str | None = None,
    to: str = "generic",
    budget_tokens: int = 4000,
) -> str:
    """Deterministic (extractive, no LLM) handoff brief over visible projects only."""
    if to not in HANDOFF_TARGETS:
        raise ValueError(f"unknown target {to!r}; use one of: {', '.join(HANDOFF_TARGETS)}")
    if budget_tokens < 1:
        raise ValueError("budget_tokens must be >= 1")
    package = collect_handoff(
        conn,
        _visible_project(conn, project),
        parse_when(since) if since else None,
        min(budget_tokens, MAX_RESPONSE_TOKENS),
        exclude_mcp=True,
    )
    return render_handoff(conn, package, to)


def tool_sync(conn: sqlite3.Connection, source: str | None = None) -> str:
    _check_source(source)
    results = run_sync(
        conn,
        available_connectors(),
        only=[source] if source else None,
        project_configs=load_projects_config(paths.config_path()),
    )
    lines = []
    for r in results:
        if r.status == "ok":
            lines.append(f"{r.source}: added {r.events_new} events, {r.sessions_new} sessions")
        elif r.status == "skipped":
            lines.append(f"{r.source}: skipped (disabled)")
        else:
            lines.append(f"{r.source}: ERROR {r.error}")
    return "\n".join(lines) or "nothing to sync"


def _wrap(fn: Callable[..., str]) -> Callable[..., str]:
    """Open a short-lived DB handle per call; redact + cap whatever the tool returns."""

    def run(*args: object, **kwargs: object) -> str:
        with closing(connect(paths.db_path())) as conn:
            try:
                return _finish(fn(conn, *args, **kwargs))
            except sqlite3.OperationalError as exc:  # e.g. FTS syntax problems
                raise ValueError(str(exc)) from exc

    return run


def build_server() -> MCPServer:
    server = MCPServer("ctx", instructions=INSTRUCTIONS)

    @server.tool()
    def search(
        query: str,
        project: str | None = None,
        source: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 10,
    ) -> str:
        """Full-text search over past agent sessions. Returns cited, redacted snippets.
        since/until accept 7d, 12h, 2026-10-01 or an ISO datetime."""
        return _wrap(tool_search)(query, project, source, since, until, limit)

    @server.tool()
    def get_context(
        query: str, project: str | None = None, budget_tokens: int = DEFAULT_BUDGET_TOKENS
    ) -> str:
        """Budgeted, cited context for a query (ctx's build_context). Redacted."""
        return _wrap(tool_get_context)(query, project, budget_tokens)

    @server.tool()
    def get_session(
        source: str, session_id: str, around_message_id: str | None = None, limit: int = 20
    ) -> str:
        """Read a window of messages from a session, optionally centered on a cited message id."""
        return _wrap(tool_get_session)(source, session_id, around_message_id, limit)

    @server.tool()
    def list_projects() -> str:
        """List projects visible through MCP, with session counts."""
        return _wrap(tool_list_projects)()

    @server.tool()
    def recent_sessions(
        project: str | None = None, source: str | None = None, limit: int = 10
    ) -> str:
        """Most recent sessions, newest first."""
        return _wrap(tool_recent_sessions)(project, source, limit)

    @server.tool()
    def timeline(
        project: str | None = None,
        source: str | None = None,
        since: str | None = None,
        until: str | None = None,
        day: str | None = None,
        tz: str | None = None,
        limit: int = 30,
    ) -> str:
        """What was the user working on? Per local day and project: time span, sources,
        session titles, message counts, top files and commands. day is today, yesterday
        or YYYY-MM-DD; since/until accept 7d or dates; tz is an IANA zone."""
        return _wrap(tool_timeline)(project, source, since, until, day, tz, limit)

    @server.tool()
    def handoff(
        project: str | None = None,
        since: str | None = None,
        to: str = "generic",
        budget_tokens: int = 4000,
    ) -> str:
        """Markdown handoff brief of recent work (state, decisions, open threads, recent
        files/commands, citations). Extractive, no LLM. to is claude-code, codex or
        generic; since accepts 7d, 12h, 2026-10-01 or an ISO datetime."""
        return _wrap(tool_handoff)(project, since, to, budget_tokens)

    @server.tool()
    def sync(source: str | None = None) -> str:
        """Import new sessions into ctx's own database (idempotent). Writes only ctx's own
        data directory; source tools' storage is read-only."""
        return _wrap(tool_sync)(source)

    return server


def serve() -> None:
    build_server().run("stdio")
