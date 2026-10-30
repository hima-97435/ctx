"""Typer CLI: `ctx sources list|show`, `sync`, `search`, `context`, `reindex`."""

import json
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import typer

from ctx.connectors.registry import available_connectors, export_connector
from ctx.core import paths
from ctx.core.context import build_context
from ctx.core.db import connect
from ctx.core.episodes import (
    build_timeline,
    local_day_bounds,
    parse_day,
    parse_local,
    rebuild_episodes,
    render_timeline,
    resolve_tz,
)
from ctx.core.projects import list_projects, load_projects_config
from ctx.core.search import parse_when, search
from ctx.core.status import collect_status, run_doctor
from ctx.core.sync import SourceResult, dry_run, rebuild_fts
from ctx.core.sync import sync as run_sync

app = typer.Typer(help="ctx: local-first personal context engine.", no_args_is_help=True)
sources_app = typer.Typer(help="Registered sources and their permission records.")
app.add_typer(sources_app, name="sources")
mcp_app = typer.Typer(help="MCP server integration (prints config; never writes it).")
app.add_typer(mcp_app, name="mcp")


project_app = typer.Typer(help="Configured projects: roots, flags and stats.")
app.add_typer(project_app, name="project")


def _fail(message: str, code: int = 2) -> typer.Exit:
    typer.echo(f"error: {message}", err=True)
    return typer.Exit(code)


def _fmt_ts(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d %H:%M")


@sources_app.command("list")
def sources_list() -> None:
    """List registered sources: enabled, allowed paths, last sync, schema version seen."""
    from ctx.core.sync import ensure_source

    conn = connect(paths.db_path())
    typer.echo(f"{'SOURCE':<18} {'ENABLED':<8} {'LAST SYNC':<26} {'SCHEMA SEEN':<22} ALLOWED PATHS")
    for connector in available_connectors().values():
        row = ensure_source(conn, connector)
        allowed = ", ".join(json.loads(row["allowed_paths"]))
        typer.echo(
            f"{row['name']:<18} {'yes' if row['enabled'] else 'no':<8} "
            f"{row['last_sync'] or '-':<26} {row['schema_version_seen'] or '-':<22} {allowed}"
        )


@sources_app.command("show")
def sources_show(name: str) -> None:
    """Show one source's permission record and its code-level denylists."""
    from ctx.core.sync import ensure_source

    connectors = available_connectors()
    if name not in connectors:
        raise _fail(f"unknown source {name!r}; known: {', '.join(connectors)}")
    connector = connectors[name]
    row = ensure_source(connect(paths.db_path()), connector)
    typer.echo(f"name:                {row['name']}")
    typer.echo(f"enabled:             {bool(row['enabled'])}")
    typer.echo(f"schema_version_pin:  {connector.schema_version_pin}")
    typer.echo(f"schema_version_seen: {row['schema_version_seen'] or '-'}")
    typer.echo(f"last_sync:           {row['last_sync'] or '-'}")
    typer.echo(f"allowed_paths:       {', '.join(json.loads(row['allowed_paths']))}")
    typer.echo(f"denylist_paths:      {', '.join(connector.denylist_paths) or '-'}")
    typer.echo(f"denylist_tables:     {', '.join(sorted(connector.denylist_tables)) or '-'}")


_STATUS_LABEL = {
    "ok": "ok",
    "skipped": "disabled",
    "not_installed": "not installed",
    "error": "ERROR",
}


def _print_results(results: list[SourceResult]) -> None:
    if any(r.dry_run for r in results):
        typer.echo("[dry-run] nothing is written; counts are what a sync would add")
    typer.echo(
        f"{'SOURCE':<18} {'STATUS':<14} {'SESSIONS+':>9} {'EVENTS+':>8} {'DUP':>6} "
        f"{'SKIPPED':>8} {'UNCHANGED':>9} {'WARNINGS':>8} SCHEMA"
    )
    for r in results:
        typer.echo(
            f"{r.source:<18} {_STATUS_LABEL.get(r.status, r.status):<14} {r.sessions_new:>9} "
            f"{r.events_new:>8} {r.events_duplicate:>6} {r.skipped_records:>8} "
            f"{r.unchanged_units:>9} {len(r.warnings):>8} {r.schema_version or '-'}"
        )
    for r in results:
        if r.status == "not_installed":
            typer.echo(f"{r.source}: not installed (no storage at {r.detail}); skipped")
        for w in r.warnings:
            typer.echo(f"{r.source}: warning: {w}")
        if r.status == "error":
            typer.echo(f"{r.source}: ERROR {r.error}", err=True)


@app.command()
def sync(
    source: list[str] = typer.Option(None, "--source", "-s", help="Only this source (repeatable)."),
    dry_run_flag: bool = typer.Option(False, "--dry-run", help="Report only; write nothing."),
    full: bool = typer.Option(
        False, "--full", help="Ignore incremental state; re-read everything."
    ),
) -> None:
    """Import enabled sources into the database (idempotent, incremental).

    Sources whose storage is absent are skipped ("not installed"); naming one with
    --source is an error. Version-pin, denylist and corrupt-data failures are errors.
    """
    connectors = available_connectors()
    try:
        if dry_run_flag:
            results = dry_run(connectors, only=source or None)
        else:
            conn = connect(paths.db_path())
            configs = load_projects_config(paths.config_path())
            results = run_sync(
                conn, connectors, only=source or None, project_configs=configs, full=full
            )
    except ValueError as exc:
        raise _fail(str(exc)) from exc
    _print_results(results)
    if any(r.status == "error" for r in results):
        raise typer.Exit(1)


@app.command("import")
def import_cmd(
    kind: str = typer.Argument(..., help="chatgpt | claude-ai | gemini"),
    path: Path = typer.Argument(..., help="Official export: a .zip or an extracted folder."),
    dry_run_flag: bool = typer.Option(False, "--dry-run", help="Report only; write nothing."),
    full: bool = typer.Option(False, "--full", help="Ignore incremental state."),
) -> None:
    """Import an official web-chat export (ChatGPT, Claude.ai, Gemini/Takeout).

    The export is read in place (zip members are never extracted) and re-importing the
    same or a newer export adds only new messages. Exports dropped in
    `<CTX_HOME>/imports/<source>/` are picked up by plain `ctx sync`.
    """
    path = path.expanduser()
    if not path.exists():
        raise _fail(f"{path} does not exist")
    try:
        connector = export_connector(kind, path.resolve())
    except ValueError as exc:
        raise _fail(str(exc)) from exc
    name = connector.name
    try:
        if dry_run_flag:
            results = dry_run({name: connector}, only=[name], source_paths={name: path.resolve()})
        else:
            conn = connect(paths.db_path())
            configs = load_projects_config(paths.config_path())
            results = run_sync(
                conn,
                {name: connector},
                only=[name],
                source_paths={name: path.resolve()},
                project_configs=configs,
                full=full,
            )
    except ValueError as exc:
        raise _fail(str(exc)) from exc
    _print_results(results)
    if any(r.status == "error" for r in results):
        raise typer.Exit(1)


@app.command("status")
def status_cmd(as_json: bool = typer.Option(False, "--json")) -> None:
    """Per-source install state, path, last sync and counts; DB path; project count."""
    data = collect_status(available_connectors(), paths.db_path())
    if as_json:
        typer.echo(json.dumps(data, indent=2, ensure_ascii=False))
        return
    typer.echo(f"database: {data['db_path']}" + ("" if data["db_exists"] else " (not created yet)"))
    typer.echo(f"projects: {data['projects']}   events: {data['events_total']}")
    typer.echo(
        f"{'SOURCE':<18} {'INSTALLED':<10} {'SESSIONS':>8} {'EVENTS':>8} {'LAST SYNC':<26} PATH"
    )
    for s in data["sources"]:
        typer.echo(
            f"{s['name']:<18} {'yes' if s['installed'] else 'no':<10} {s['sessions']:>8} "
            f"{s['events']:>8} {s['last_sync'] or '-':<26} {s['path']}"
        )


@app.command("doctor")
def doctor_cmd(as_json: bool = typer.Option(False, "--json")) -> None:
    """Check FTS integrity, schema version, FTS5 support and a writable data dir."""
    checks = run_doctor(paths.db_path())
    if as_json:
        typer.echo(json.dumps([c.as_dict() for c in checks], indent=2, ensure_ascii=False))
    else:
        for c in checks:
            typer.echo(f"[{c.status}] {c.name}: {c.detail}")
            if c.fix and c.status != "ok":
                typer.echo(f"      fix: {c.fix}")
    if any(c.status == "fail" for c in checks):
        raise typer.Exit(1)


@app.command("search")
def search_cmd(
    query: str,
    project: str = typer.Option(None, "--project", "-p", help="Project id or name."),
    source: str = typer.Option(None, "--source", "-s"),
    since: str = typer.Option(None, "--since", help="7d, 12h, 2026-10-01 or ISO datetime."),
    until: str = typer.Option(None, "--until"),
    all_projects: bool = typer.Option(False, "--all", help="Include non-allowed projects."),
    limit: int = typer.Option(20, "--limit", "-n", min=1),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Full-text search with citations (source, session, message, timestamp)."""
    conn = _open_db_or_fail()
    try:
        hits = search(
            conn,
            query,
            project=project,
            source=source,
            since=parse_when(since) if since else None,
            until=parse_when(until, end_of_day=True) if until else None,
            include_all=all_projects,
            limit=limit,
        )
    except (ValueError, sqlite3.OperationalError) as exc:
        raise _fail(str(exc)) from exc
    if as_json:
        typer.echo(json.dumps([asdict(h) for h in hits], indent=2, ensure_ascii=False))
        return
    if not hits:
        typer.echo("no results")
        return
    for h in hits:
        c = h.citation
        typer.echo(
            f"{c.source} session={c.source_session_id or '-'} message={c.message_id or '-'} "
            f"{_fmt_ts(c.ts)} project={c.project_id or '-'} [{h.kind}]"
        )
        typer.echo(f"  {h.snippet}")


@app.command("context")
def context_cmd(
    query: str,
    project: str = typer.Option(None, "--project", "-p"),
    budget: int = typer.Option(2000, "--budget", "-b", min=1, help="Token budget."),
    all_projects: bool = typer.Option(False, "--all"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Assemble budgeted, cited context for a query (the build_context seam)."""
    conn = _open_db_or_fail()
    try:
        package = build_context(conn, query, budget, project, include_all=all_projects)
    except (ValueError, sqlite3.OperationalError) as exc:
        raise _fail(str(exc)) from exc
    if as_json:
        data = asdict(package)
        data["evidence_event_ids"] = package.evidence_event_ids
        typer.echo(json.dumps(data, indent=2, ensure_ascii=False))
        return
    typer.echo(package.render())
    typer.echo(
        f"\n-- {len(package.items)} items, {package.tokens_used}/{package.budget_tokens} tokens, "
        f"{len(package.left_out)} left out"
    )


@app.command()
def reindex(
    gap: int = typer.Option(
        None,
        "--gap",
        min=1,
        help="Idle gap, minutes (default 45).",
    ),
) -> None:
    """Rebuild the FTS index and the episodes from events alone."""
    conn = _open_db_or_fail()
    try:
        n_events = rebuild_fts(conn)
        n_episodes = rebuild_episodes(conn, gap)
    except ValueError as exc:
        raise _fail(str(exc)) from exc
    typer.echo(f"reindexed {n_events} events, {n_episodes} episodes")


@app.command("timeline")
def timeline_cmd(
    project: str = typer.Option(None, "--project", "-p", help="Project id or name."),
    source: str = typer.Option(None, "--source", "-s"),
    since: str = typer.Option(None, "--since", help="7d, 2026-10-01 or ISO datetime (local)."),
    until: str = typer.Option(None, "--until"),
    day: str = typer.Option(None, "--day", help="today, yesterday or YYYY-MM-DD (local)."),
    tz: str = typer.Option(None, "--tz", help="IANA zone (default $CTX_TZ, else system)."),
    all_projects: bool = typer.Option(False, "--all", help="Include non-allowed/unlinked."),
    limit: int = typer.Option(100, "--limit", "-n", min=1, help="Most recent N entries."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """What was I working on? Per day and project, across all sources."""
    conn = _open_db_or_fail()
    try:
        zone = resolve_tz(tz)
        if day and (since or until):
            raise ValueError("--day cannot be combined with --since/--until")
        if day:
            lo, hi = local_day_bounds(parse_day(day, zone), zone)
        else:
            lo = parse_local(since, zone) if since else None
            hi = parse_local(until, zone, end_of_day=True) if until else None
        entries = build_timeline(
            conn,
            zone,
            project=project,
            source=source,
            since=lo,
            until=hi,
            include_all=all_projects,
            limit=limit,
        )
    except (ValueError, sqlite3.OperationalError) as exc:
        raise _fail(str(exc)) from exc
    if as_json:
        typer.echo(
            json.dumps(
                {"timezone": str(zone), "entries": [asdict(e) for e in entries]},
                indent=2,
                ensure_ascii=False,
            )
        )
        return
    typer.echo(render_timeline(entries, zone))


def _fmt_local(ms: int | None, zone) -> str:
    if ms is None:
        return "-"
    return datetime.fromtimestamp(ms / 1000, zone).strftime("%Y-%m-%d %H:%M")


@project_app.command("list")
def project_list(
    tz: str = typer.Option(None, "--tz"), as_json: bool = typer.Option(False, "--json")
) -> None:
    """List projects with allow / local_only / mcp_exclude state and stats."""
    conn = _open_db_or_fail()
    try:
        zone = resolve_tz(tz)
    except ValueError as exc:
        raise _fail(str(exc)) from exc
    projects = list_projects(conn)
    if as_json:
        typer.echo(json.dumps(projects, indent=2, ensure_ascii=False))
        return
    if not projects:
        typer.echo("no projects (add them to config/projects.toml, then `ctx sync`)")
        return
    typer.echo(
        f"{'ID':<16} {'NAME':<20} {'ALLOWED':<8} {'LOCAL':<6} {'MCP':<5} "
        f"{'SESS':>5} {'EVENTS':>7} {'EPIS':>5} LAST ACTIVITY"
    )
    for p in projects:
        typer.echo(
            f"{p['id']:<16} {p['name']:<20} {'yes' if p['allowed'] else 'no':<8} "
            f"{'yes' if p['local_only'] else 'no':<6} {'no' if p['mcp_exclude'] else 'yes':<5} "
            f"{p['sessions']:>5} {p['events']:>7} {p['episodes']:>5} "
            f"{_fmt_local(p['last_ts'], zone)}"
        )


@project_app.command("show")
def project_show(
    name: str,
    tz: str = typer.Option(None, "--tz"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Show one project: roots, flags, per-source counts and activity span."""
    conn = _open_db_or_fail()
    try:
        zone = resolve_tz(tz)
        from ctx.core.search import resolve_project

        pid = resolve_project(conn, name)
    except ValueError as exc:
        raise _fail(str(exc)) from exc
    info = next(p for p in list_projects(conn) if p["id"] == pid)
    if as_json:
        typer.echo(json.dumps(info, indent=2, ensure_ascii=False))
        return
    typer.echo(f"id:           {info['id']}")
    typer.echo(f"name:         {info['name']}")
    typer.echo(f"allowed:      {info['allowed']}")
    typer.echo(f"local_only:   {info['local_only']}")
    typer.echo(f"mcp_exclude:  {info['mcp_exclude']}")
    typer.echo(f"roots:        {', '.join(info['roots']) or '-'}")
    typer.echo(f"sessions:     {info['sessions']}")
    typer.echo(f"events:       {info['events']}")
    typer.echo(f"episodes:     {info['episodes']}")
    typer.echo(f"first:        {_fmt_local(info['first_ts'], zone)}")
    typer.echo(f"last:         {_fmt_local(info['last_ts'], zone)}")
    per_source = ", ".join(f"{k}={v}" for k, v in info["events_by_source"].items())
    typer.echo(f"by source:    {per_source or '-'}")


@app.command("serve-mcp")
def serve_mcp() -> None:
    """Run the read-only MCP server on stdio (for coding agents)."""
    from ctx.mcp.server import serve

    serve()


@mcp_app.command("config")
def mcp_config(
    client: str = typer.Option(
        ..., "--client", "-c", help="claude-code|codex|opencode|cursor|gemini|hermes|generic"
    ),
) -> None:
    """Print the config snippet/command for a client. Does not modify any file."""
    from ctx.mcp.config import snippet

    try:
        typer.echo(snippet(client))
    except ValueError as exc:
        raise _fail(str(exc)) from exc


def _open_db_or_fail() -> sqlite3.Connection:
    db = paths.db_path()
    if not Path(db).exists():
        raise _fail(f"no database at {db}; run `ctx sync` first", 1)
    return connect(db)


if __name__ == "__main__":
    app()
