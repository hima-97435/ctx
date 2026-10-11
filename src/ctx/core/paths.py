"""Filesystem locations for ctx runtime data.

All paths derive from the repo root (the directory containing pyproject.toml).
`CTX_DATA_DIR` overrides the data directory — used by tests and for sandboxed runs.
"""

import os
from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def data_dir() -> Path:
    override = os.environ.get("CTX_DATA_DIR")
    if override:
        return Path(override).resolve()
    return repo_root() / "data"


def db_path() -> Path:
    return data_dir() / "ctx.db"


def blobs_dir() -> Path:
    return data_dir() / "blobs"
