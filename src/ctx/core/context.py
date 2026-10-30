"""The ONE prompt-assembly path (AGENTS rule 8).

`build_context` selects top-N hits (via `retrieve`: hybrid when embeddings exist, else
FTS5) and trims them to a token budget (v1, deliberately dumb; smarter strategies swap in
behind this signature). No LLM is
called. Every item carries its evidence event id and a citation; whatever did not
fit is reported in `left_out` so callers can fetch detail on demand.

Budget accounting counts item text only (not the citation header). Item text is
re-redacted before it is returned (ADR-012).
"""

import sqlite3
from dataclasses import dataclass, field

from ctx.core.embed import Embedder
from ctx.core.retrieve import retrieve
from ctx.core.search import Citation
from ctx.core.tokens import estimate_tokens
from ctx.core.truncate import truncate_to_tokens

DEFAULT_TOP_N = 50


@dataclass(frozen=True)
class ContextItem:
    text: str
    type: str  # events.kind
    evidence_event_ids: list[int]
    citation: Citation
    token_estimate: int
    truncated: bool = False


@dataclass(frozen=True)
class LeftOut:
    event_id: int
    token_estimate: int
    reason: str  # 'over_budget'


@dataclass
class ContextPackage:
    query: str
    project: str | None
    budget_tokens: int
    items: list[ContextItem] = field(default_factory=list)
    tokens_used: int = 0
    left_out: list[LeftOut] = field(default_factory=list)

    @property
    def evidence_event_ids(self) -> list[int]:
        return [i for item in self.items for i in item.evidence_event_ids]

    def render(self) -> str:
        """Plain-text block with a citation line per item (what callers hand to an LLM)."""
        blocks = []
        for n, item in enumerate(self.items, 1):
            c = item.citation
            ref = f"{c.source} session={c.source_session_id or '-'} message={c.message_id or '-'}"
            blocks.append(f"[{n}] ({ref} event={c.event_id} ts={c.ts} {item.type})\n{item.text}")
        return "\n\n".join(blocks)


def build_context(
    conn: sqlite3.Connection,
    query: str,
    budget_tokens: int,
    project: str | None = None,
    *,
    include_all: bool = False,
    exclude_mcp: bool = False,
    top_n: int = DEFAULT_TOP_N,
    mode: str = "auto",
    embedder: Embedder | None = None,
) -> ContextPackage:
    package = ContextPackage(query=query, project=project, budget_tokens=budget_tokens)
    hits = retrieve(
        conn,
        query,
        mode=mode,
        embedder=embedder,
        project=project,
        include_all=include_all,
        exclude_mcp=exclude_mcp,
        limit=top_n,
        include_text=True,
    )
    remaining = budget_tokens
    for hit in hits:
        text = hit.text or ""
        cost = estimate_tokens(text)
        eid = hit.citation.event_id
        if cost <= remaining:
            item_text, truncated = text, False
        elif not package.items and remaining > 0:
            # Never return nothing when something matched: trim the best hit to fit.
            item_text, truncated = truncate_to_tokens(text, remaining), True
        else:
            package.left_out.append(LeftOut(eid, cost, "over_budget"))
            continue
        used = estimate_tokens(item_text)
        remaining -= used
        package.tokens_used += used
        package.items.append(ContextItem(item_text, hit.kind, [eid], hit.citation, used, truncated))
    return package
