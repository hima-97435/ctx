# Context Engineering (deferred work)

This file is mostly a placeholder. The techniques in the table below are **not implemented**
(hybrid retrieval and the eval harness are; see below and ADR-027/028).
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

## Eval harness (M4) and writing a personal eval set

`ctx eval run FILE [--mode all|fts|vector|hybrid] [--k 10] [--model SPEC] [--all] [--json]`
runs every question through `retrieve` (the ranking path under `build_context`, MCP and
`ctx search`) and prints recall@k, MRR and nDCG@k per mode plus the first relevant rank per
question. `--mode all` (default) skips vector/hybrid with a note when no embeddings exist.
Metrics are binary-relevance and averaged over questions; each expected citation is
credited once, so extra hits in the same session do not inflate nDCG.

The file is JSONL (one object per line; blank lines and `#` comments allowed):

```json
{"id": "why-sqlite", "question": "why did I choose SQLite for the job queue",
 "filters": {"project": "alpha", "source": "claude_code", "since": "90d", "until": "2026-10-31"},
 "expect": [{"source": "claude_code", "session_id": "cc3", "message_id": "m2"}],
 "expect_contains": ["single-node"]}
```

`expect` (required) lists citations: `source` + `session_id` (the source's own session id,
as `ctx search` prints `session=`), `message_id` optional (omit to accept any event of
that session). `filters` take the same values as `ctx search`. `expect_contains` is an
optional extra check reported as `contains@k`.

**Your own set (never committed):** keep it under your data dir, e.g.
`<CTX_HOME>/eval/personal.jsonl` (`$CTX_HOME`, else the per-OS dir listed in
`docs/SCHEMA.md`), which is outside the repo and gitignored by construction. Write 15-20
questions you really asked yourself, including paraphrases ("how did I speed up X"),
exact error strings, file names and "why did I choose X" decisions. Find the answer with
`ctx search ... --json` (use the `source`, `source_session_id`, `message_id` of the event
that holds the answer), record it as `expect`, then run
`ctx eval run "$CTX_HOME/eval/personal.jsonl" --all` against your real database (add
`--all` if answers live in unlinked web chats). Do not copy real questions or answers
into the repo; the committed synthetic set is `tests/fixtures/eval_synthetic.jsonl`
(corpus in `tests/fixtures/eval_corpus.py`).

## Adoption gate

No technique is adopted until it is **validated against the eval set**: 15–20 real
questions from my history with known answers and known source events
(milestone M4). Each candidate must beat the current v1 behavior on that eval set
before it replaces the dumb implementation.

The user maintains separate notes on context-engineering techniques; they will be
incorporated when this phase starts (do not ask for them earlier).
