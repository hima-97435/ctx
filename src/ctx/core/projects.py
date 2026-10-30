"""Project config loading and deterministic cwd -> project linking (ADR-009/010).

Linking is pure string logic: the project whose root is the LONGEST path-prefix of
the working directory wins. No filesystem access, no LLM.
"""

import os
import sqlite3
import tomllib
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field


class ProjectConfig(BaseModel):
    id: str
    name: str
    allowed: bool = False
    local_only: bool = True
    mcp_exclude: bool = False
    roots: list[str] = Field(default_factory=list)


def load_projects_config(path: Path | None) -> list[ProjectConfig]:
    """Parse config/projects.toml. A missing file (None) means no projects."""
    if path is None:
        return []
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    projects = []
    for project_id, body in (raw.get("projects") or {}).items():
        projects.append(
            ProjectConfig(
                id=project_id,
                name=body.get("name", project_id),
                **{k: v for k, v in body.items() if k != "name"},
            )
        )
    return projects


def normalize_path(path: str) -> str:
    """Canonical string form for prefix comparison (slashes, trailing slash, case on Windows)."""
    p = path.replace("\\", "/")
    if len(p) > 1:
        p = p.rstrip("/") or "/"
    return os.path.normcase(p).replace("\\", "/") if os.name == "nt" else p


def link_project(cwd: str | None, roots: list[tuple[str, str]]) -> str | None:
    """Return the project id whose root is the longest prefix of `cwd`, else None.

    `roots` is a list of (project_id, root_path). Matching respects path boundaries
    (`/a/bc` is not under `/a/b`).
    """
    if not cwd:
        return None
    target = normalize_path(cwd)
    best_id: str | None = None
    best_len = -1
    for project_id, root in roots:
        norm = normalize_path(root)
        under = target == norm or target.startswith(norm if norm.endswith("/") else norm + "/")
        if under and len(norm) > best_len:
            best_id, best_len = project_id, len(norm)
    return best_id


def sync_projects(conn: sqlite3.Connection, configs: list[ProjectConfig]) -> None:
    """Make projects/project_roots mirror the config (config overrides the DB)."""
    now = datetime.now(UTC).isoformat(timespec="seconds")
    conn.execute("BEGIN")
    try:
        for cfg in configs:
            conn.execute(
                "INSERT INTO projects(id, name, local_only, allowed, mcp_exclude, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET name=excluded.name,"
                " local_only=excluded.local_only, allowed=excluded.allowed,"
                " mcp_exclude=excluded.mcp_exclude",
                (
                    cfg.id,
                    cfg.name,
                    int(cfg.local_only),
                    int(cfg.allowed),
                    int(cfg.mcp_exclude),
                    now,
                ),
            )
            conn.execute("DELETE FROM project_roots WHERE project_id = ?", (cfg.id,))
            conn.executemany(
                "INSERT OR IGNORE INTO project_roots(project_id, root_path) VALUES (?, ?)",
                [(cfg.id, root) for root in cfg.roots],
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def relink_sessions(conn: sqlite3.Connection) -> None:
    """Re-derive every session's project from its stored cwd (config may have changed).

    Incremental sync does not re-read unchanged sources, so linking cannot depend on
    sessions being re-yielded. Events keep the project they were stored with.
    """
    roots = load_roots(conn)
    conn.execute("BEGIN")
    try:
        rows = conn.execute("SELECT id, cwd, project_id FROM sessions WHERE cwd IS NOT NULL")
        for row in rows.fetchall():
            project = link_project(row["cwd"], roots)
            if project != row["project_id"]:
                conn.execute(
                    "UPDATE sessions SET project_id = ? WHERE id = ?", (project, row["id"])
                )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def load_roots(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    return [(r["project_id"], r["root_path"]) for r in conn.execute("SELECT * FROM project_roots")]


def project_stats(conn: sqlite3.Connection, project_id: str | None) -> dict:
    """Counts for one project (None = events linked to no project), derived from the DB."""
    cond = "IS NULL" if project_id is None else "= ?"
    args: tuple = () if project_id is None else (project_id,)
    ev = (
        "FROM events e LEFT JOIN sessions s ON s.id = e.session_id"
        f" WHERE COALESCE(s.project_id, e.project_id) {cond}"
    )
    n_sessions = conn.execute(
        f"SELECT COUNT(*) FROM sessions WHERE project_id {cond}", args
    ).fetchone()[0]
    n_events, first, last = conn.execute(
        f"SELECT COUNT(*), MIN(e.ts), MAX(e.ts) {ev}", args
    ).fetchone()
    by_source = {
        r["source"]: r["n"]
        for r in conn.execute(f"SELECT e.source, COUNT(*) AS n {ev} GROUP BY e.source", args)
    }
    n_episodes = conn.execute(
        f"SELECT COUNT(*) FROM episodes WHERE project_id {cond}", args
    ).fetchone()[0]
    return {
        "sessions": n_sessions,
        "events": n_events,
        "episodes": n_episodes,
        "first_ts": first,
        "last_ts": last,
        "events_by_source": dict(sorted(by_source.items())),
    }


def list_projects(conn: sqlite3.Connection) -> list[dict]:
    """Every configured project with flags, roots and stats, ordered by id."""
    out = []
    for p in conn.execute("SELECT * FROM projects ORDER BY id").fetchall():
        roots = [
            r["root_path"]
            for r in conn.execute(
                "SELECT root_path FROM project_roots WHERE project_id = ? ORDER BY root_path",
                (p["id"],),
            )
        ]
        out.append(
            {
                "id": p["id"],
                "name": p["name"],
                "allowed": bool(p["allowed"]),
                "local_only": bool(p["local_only"]),
                "mcp_exclude": bool(p["mcp_exclude"]),
                "roots": roots,
                **project_stats(conn, p["id"]),
            }
        )
    return out


def set_cloud_policy(conn: sqlite3.Connection, name: str, *, allow_cloud: bool) -> str:
    """Flip `projects.local_only` in ctx's own DB (never touches files outside it).

    `unlinked` is the reserved pseudo-project that governs events linked to no project
    (web-chat imports); it is created on first use. Returns the project id. Note that
    `sync_projects` re-applies `config/projects.toml` on every sync (config wins), so a
    project defined there must be changed there too to persist.
    """
    from ctx.core.search import resolve_project
    from ctx.llm.policy import UNLINKED_PROJECT_ID

    if name == UNLINKED_PROJECT_ID:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        conn.execute(
            "INSERT OR IGNORE INTO projects(id, name, local_only, allowed, created_at)"
            " VALUES (?, ?, 1, 0, ?)",
            (UNLINKED_PROJECT_ID, UNLINKED_PROJECT_ID, now),
        )
        pid = UNLINKED_PROJECT_ID
    else:
        pid = resolve_project(conn, name)
    conn.execute("UPDATE projects SET local_only = ? WHERE id = ?", (0 if allow_cloud else 1, pid))
    return pid
