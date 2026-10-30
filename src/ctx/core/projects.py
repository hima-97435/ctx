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


def load_roots(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    return [(r["project_id"], r["root_path"]) for r in conn.execute("SELECT * FROM project_roots")]
