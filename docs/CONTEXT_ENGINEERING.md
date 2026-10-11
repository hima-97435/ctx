# Context Engineering (deferred work)

This file is a placeholder. The techniques below are **not implemented** in v1.
v1 builds only the *seam* (`build_context`) so they can be added later without a
rewrite (`docs/ARCHITECTURE.md`).

## Open hypothesis

**Answer quality degrades as the context window fills.** Naively stuffing more
retrieved context into a prompt is assumed to eventually hurt precision, dilute
attention, and increase cost. This hypothesis must be *measured*, not assumed —
see the eval gate below.

## Planned techniques (all deferred)

| Technique | Idea | Where it plugs in |
|---|---|---|
| Dedup & reranking | Collapse near-duplicate hits; reorder by relevance before trimming | inside `build_context` |
| Token-budget allocation | Split `budget_tokens` across facts / summaries / raw excerpts instead of greedy top-N | inside `build_context` |
| Hierarchical summarization | Episode summaries above raw events; drill down via evidence IDs | synthesis layer → `build_context` |
| Rolling summaries | Update episode summaries incrementally instead of full re-summarization | synthesis layer |
| Progressive disclosure (MCP) | Return terse results + evidence IDs first; fetch detail on demand | `ctx serve-mcp` |

## Adoption gate

No technique is adopted until it is **validated against the eval set**: 15–20 real
questions from my history with known answers and known source events
(milestone M4). Each candidate must beat the current v1 behavior on that eval set
before it replaces the dumb implementation.

The user maintains separate notes on context-engineering techniques; they will be
incorporated when this phase starts (do not ask for them earlier).
