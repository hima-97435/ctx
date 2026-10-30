"""The cloud policy gate (AGENTS rule 9): the ONE choke point for sending text to an LLM.

A provider's `chat()` refuses to run without a `Clearance`, and a Clearance is minted only
by `authorize()`, which looks at every project whose text is in the request:

- a local provider (Ollama, or an OpenAI-compatible server on loopback) is always cleared;
- a cloud provider is cleared only if the scope is non-empty and EVERY project in it has
  `local_only=0`. One `local_only` project in a mixed scope refuses the whole request.
- evidence linked to no project (e.g. web-chat exports) counts as `local_only` unless the
  user opted in with the reserved pseudo-project `unlinked` (`ctx project set unlinked
  --allow-cloud`).

Cloud embedders use the same predicate (`cloud_scope_refusal`) through
`core.embed.check_embedder_allowed` and the `embed_events` SQL filter.
"""

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

UNLINKED_PROJECT_ID = "unlinked"
_MINT = object()


class CloudRefused(PermissionError):
    """A cloud call was refused by policy (rule 9)."""


class _HasCloud(Protocol):
    name: str
    is_cloud: bool


@dataclass(frozen=True)
class Clearance:
    """Proof that `authorize()` approved this provider for this project scope."""

    provider: str
    projects: frozenset[str | None]
    _mint: object = None

    def __post_init__(self) -> None:
        if self._mint is not _MINT:
            raise TypeError("Clearance can only be issued by ctx.llm.policy.authorize()")


def cloud_scope_refusal(conn: sqlite3.Connection, project_ids: Iterable[str | None]) -> str | None:
    """Why a cloud call over these projects is refused, or None if it is allowed."""
    scope = set(project_ids)
    if not scope:
        return "no project scope: cloud use needs evidence from at least one local_only=0 project"
    blocked = []
    for pid in sorted(scope, key=lambda p: p or ""):
        lookup = UNLINKED_PROJECT_ID if pid is None else pid
        row = conn.execute("SELECT local_only FROM projects WHERE id = ?", (lookup,)).fetchone()
        if row is None or row["local_only"]:
            blocked.append(UNLINKED_PROJECT_ID + " (no project)" if pid is None else pid)
    if blocked:
        return (
            f"local_only project(s) in scope: {', '.join(blocked)}; "
            "opt in with `ctx project set NAME --allow-cloud` or use a local provider"
        )
    return None


def authorize(
    conn: sqlite3.Connection, provider: _HasCloud, project_ids: Iterable[str | None]
) -> Clearance:
    """Raise CloudRefused, or return the Clearance that `provider.chat()` requires."""
    scope = frozenset(project_ids)
    if provider.is_cloud:
        why = cloud_scope_refusal(conn, scope)
        if why is not None:
            raise CloudRefused(f"cloud provider {provider.name!r} refused: {why}")
    return Clearance(provider.name, scope, _mint=_MINT)


def require_clearance(clearance: object, provider_name: str) -> None:
    """Called first thing by every provider `chat()`."""
    if not isinstance(clearance, Clearance) or clearance.provider != provider_name:
        raise CloudRefused(f"{provider_name}: call refused, no policy clearance (rule 9)")
