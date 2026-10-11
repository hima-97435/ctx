# Roadmap

Each milestone has a checkbox acceptance test. A milestone is done only when every
box is checked **and** the relevant docs are updated. Later milestones are not
started without explicit go-ahead.

## M0 — Docs & scaffolding (done)

- [x] Repo bootstrapped (uv, git, .gitignore, config/projects.toml seeded)
- [x] Phase 0 machine investigation recorded (`docs/CONNECTORS.md`)
- [x] Vision, architecture, schema, decisions documented

## M1 — OpenCode import, search, context seam

Done when all of these pass with tests:

- [ ] `ctx sync` imports all existing OpenCode sessions (47 sessions / 1,618
      messages / 6,093 parts) into SQLite, linked to projects via cwd roots.
- [ ] A second `ctx sync` creates **zero duplicates** (contract test).
- [ ] `ctx search "phrase"` returns excerpts with session/message references and
      respects `--project`, `--since`, `--source`, `--all`.
- [ ] Redaction runs **before** indexing; unit test with fake secrets (API keys,
      bearer tokens, `.env` values, private keys) proves none survive into
      `events`/FTS.
- [ ] Tool outputs > 64 KB are truncated (head+tail) with a blob reference written
      **before** the event insert; every event has `token_estimate`.
- [ ] Prompt-history connector imports `prompt-history.jsonl` as `kind='prompt'` events.
- [ ] Source permission records work: `ctx sources list | add | show` (enabled,
      allowed paths, last sync, schema version seen); denylisted tables/paths are
      never read (contract test).
- [ ] OpenCode connector fails loudly on unrecognized schema/version, before any writes.
- [ ] Shared connector contract suite exists and the OpenCode connectors pass it.
- [ ] Fixture-based test uses a small anonymized sample of the real session format.
- [ ] `build_context(query, budget_tokens, project)` exists with v1 behavior
      (top-N FTS5 trimmed to budget) and a unit test; returns evidence IDs and
      what was left out.

## M2 — Timeline & episodes (deterministic, no LLM)

- [ ] Episodes computed deterministically (same project + < 45 min idle gap,
      configurable).
- [ ] `ctx timeline [--since ...] [--project X]` renders a browsable history with
      per-episode event counts and top terms.
- [ ] `ctx project list | show <name>` shows roots, allow/local_only state, stats.

## M3 — Handoff

- [ ] `ctx handoff <project>` emits a concise Markdown context package (state,
      open TODOs, recent decisions) built **only** via `build_context`.

## M4 — Eval set

- [ ] 15–20 real questions from my history with known answers + source event IDs
      recorded in-repo (anonymized).
- [ ] A harness scores any `build_context` strategy against the set.

## M5 — Synthesis with evidence links

- [ ] LLM provider abstraction: `LocalProvider` (Ollama) + `OpenAICompatibleProvider`
      (NVIDIA; base URL/key from `.env`, never committed).
- [ ] Episode summaries + fact extraction, every fact carrying `evidence_event_ids`
      and `status ∈ {suggested, confirmed, abandoned}`.
- [ ] Cloud calls only for projects with `local_only=0` (all start `local_only=1`).
- [ ] Derived tables provably rebuildable from `events` alone.

## M6 — MCP server

- [ ] `ctx serve-mcp` exposes search/timeline/context to other agents via MCP,
      responses only through `build_context` (progressive disclosure allowed later).

## M7 — Antigravity connector

- [ ] Connector #2 on the same contract (summaries DB + brain artifacts first,
      protobuf `steps` later); passes the shared contract suite.

## M8 — VS Code + shell history connectors

- [ ] As `docs/CONNECTORS.md` §4–5; pass contract suite.

## M9 — Context engineering behind the seam

- [ ] Techniques from `docs/CONTEXT_ENGINEERING.md` adopted only after beating v1
      on the M4 eval set.

## M10 — GUI (only if the CLI proves useful)

- [ ] Explicit go-ahead required before any GUI work.
