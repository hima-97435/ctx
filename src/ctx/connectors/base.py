"""Shared connector plumbing: errors and denylist enforcement helpers.

Denylists are declared per connector as constants and enforced here + in the
sync engine (ADR-014). Every connector must pass tests/connector_contract.py.
"""

import fnmatch
import re
from pathlib import Path


class ConnectorError(Exception):
    """Raised when a source cannot be read safely (schema drift, missing files)."""


class DenylistViolation(ConnectorError):
    """Raised when an access would touch a denylisted table or path."""


def assert_tables_allowed(sql: str, denylist_tables: frozenset[str] | set[str]) -> None:
    """Raise DenylistViolation if a SQL statement references a denylisted table."""
    for table in sorted(denylist_tables):
        if re.search(rf"\b{re.escape(table)}\b", sql, re.IGNORECASE):
            raise DenylistViolation(f"query references denylisted table {table!r}: {sql[:120]!r}")


def assert_path_allowed(
    path: str | Path, root: str | Path, denylist_patterns: tuple[str, ...]
) -> None:
    """Raise unless `path` lives under `root` and matches no denylist pattern.

    Denylist patterns are fnmatch globs matched against the path relative to
    `root` (e.g. "auth.json", "plugins/**/mcp_config.json") and against the
    bare file name.
    """
    resolved = Path(path).resolve()
    root_resolved = Path(root).resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise ConnectorError(f"path {resolved} is outside allowed root {root_resolved}")
    try:
        rel = resolved.relative_to(root_resolved).as_posix()
    except ValueError:  # pragma: no cover - guaranteed by the parents check above
        rel = resolved.name
    for pattern in denylist_patterns:
        if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(resolved.name, pattern):
            raise DenylistViolation(f"path {rel!r} matches denylist pattern {pattern!r}")
