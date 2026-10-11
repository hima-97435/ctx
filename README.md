# ctx

A **local-first personal context engine**. ctx collects your scattered work context —
coding-agent sessions (OpenCode today; Antigravity, VS Code, shell history later) —
into one SQLite database on your machine, and builds search, a timeline,
evidence-backed Q&A, and agent handoffs on top of it.

## The problem

Work context lives in tool silos. There is no single place to answer:

- "What was I working on last Tuesday?"
- "Why did I choose this architecture?"
- "Where did I hit this error, and how did I fix it?"

## How it works

Connectors import raw events (messages, tool calls, prompts) from each source into an
immutable, append-only store. Records are linked to projects **deterministically** —
via working directories and project roots, never by an LLM. An FTS5 index makes
everything searchable with citations back to the exact session and message. Derived
knowledge (episodes, facts) is rebuildable, evidence-linked, and strictly separated
from raw data. Anything that assembles context for an LLM goes through one seam
(`build_context`), so smarter retrieval can be added later without a rewrite.

## Install

```sh
brew install uv     # or: pip install uv
uv sync             # creates .venv, installs deps, writes uv.lock
```

## Quick start (lands with Milestone 1)

```sh
ctx sources list              # registered sources and their permissions
ctx sync                      # import all enabled sources (idempotent)
ctx search "redaction pipeline" --project ctx
```

CLI surface grows per milestone — see `docs/ROADMAP.md`.

## Privacy

- Everything stays on your machine. `data/` is gitignored and never committed.
- Secrets (API keys, tokens, `.env` values, private keys) are redacted **before**
  anything is indexed or sent to a model.
- Cloud LLM calls are per-project opt-in: every project starts `local_only=1`.
- Connectors carry hard, in-code denylists (paths and tables they must never read).

## Current status

| Milestone | Status |
|---|---|
| M0 — docs & scaffolding | done |
| M1 — OpenCode import, search, `build_context` | next |
| M2+ — timeline, handoff, synthesis, MCP | planned |

See `docs/ROADMAP.md` for acceptance tests per milestone.

## Repository layout

```
src/ctx/            package (core, connectors, cli)
tests/              pytest suite + real-format fixtures
config/projects.toml  project roots, grouping, allow overrides
data/               runtime database + blobs (gitignored, never committed)
docs/               vision, architecture, schema, connectors, roadmap, decisions
```

## Docs

`docs/VISION.md` · `docs/ARCHITECTURE.md` · `docs/SCHEMA.md` · `docs/CONNECTORS.md` ·
`docs/CONTEXT_ENGINEERING.md` · `docs/ROADMAP.md` · `docs/DECISIONS.md` · `AGENTS.md`
