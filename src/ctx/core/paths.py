"""Filesystem locations for ctx runtime data.

Data directory resolution (first match wins):
1. `CTX_HOME` — explicit override (tests use this with tmp_path).
2. `CTX_DATA_DIR` — legacy override, kept for compatibility.
3. Per-OS user data dir, independent of where the repo is checked out:
   Windows `%LOCALAPPDATA%\\ctx`, macOS `~/Library/Application Support/ctx`,
   elsewhere `$XDG_DATA_HOME/ctx` (default `~/.local/share/ctx`).
"""

import os
import sys
from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def default_data_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "ctx"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "ctx"
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "share") / "ctx"


def data_dir() -> Path:
    for var in ("CTX_HOME", "CTX_DATA_DIR"):
        override = os.environ.get(var)
        if override:
            return Path(override).resolve()
    return default_data_dir()


def db_path() -> Path:
    return data_dir() / "ctx.db"


def blobs_dir() -> Path:
    return data_dir() / "blobs"


def tmp_dir() -> Path:
    return data_dir() / "tmp"


def config_path() -> Path | None:
    """Project config: `CTX_CONFIG`, else `<data dir>/projects.toml`, else the repo's
    `config/projects.toml` (development checkout). None when no file exists."""
    override = os.environ.get("CTX_CONFIG")
    candidates = [Path(override)] if override else []
    candidates += [data_dir() / "projects.toml", repo_root() / "config" / "projects.toml"]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None
