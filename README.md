# ctx

A **local-first personal context engine**. ctx collects your scattered work context —
coding-agent sessions (OpenCode, Claude Code, Codex CLI, Hermes, Devin CLI and the Antigravity CLI today, plus ChatGPT/Claude.ai/Gemini via their official data exports; Gemini CLI, Antigravity IDE, VS Code, shell history later) —
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
uv sync --extra embed   # optional: local embeddings (model2vec + numpy) for vector/hybrid search
```

The base install is FTS5-only and needs nothing else. With the `embed` extra,
`ctx embed` builds a derived, rebuildable `embeddings` table from redacted text using a
small local model (no torch, no cloud; the first run downloads the model files from the
Hugging Face hub into its normal cache), and search becomes hybrid (FTS5 + vector,
fused with Reciprocal Rank Fusion).

## Quick start

```sh
ctx sources list              # registered sources and their permissions
ctx sync --dry-run            # report what would be imported; writes nothing
ctx sync                      # import all enabled sources (idempotent, incremental)
ctx sync --source hermes      # one source; an absent (not installed) one is an error
ctx sync --full               # ignore incremental state and re-read everything
ctx status [--json]           # per source: installed?, path, last sync, sessions/events; DB path
ctx doctor [--json]           # FTS integrity, schema version, FTS5, writable data dir (+ fixes)
ctx search "redaction pipeline" --project ctx [--source S --since 7d --until DATE --all --limit N --mode auto|fts|vector|hybrid --json]
ctx embed [--rebuild] [--model SPEC]   # build/refresh embeddings (needs the `embed` extra); incremental by text hash
ctx eval run FILE [--mode all|fts|vector|hybrid] [--k 10] [--model SPEC] [--all] [--json]   # recall@k / MRR / nDCG@k
ctx context "redaction pipeline" --project ctx --budget 2000   # budgeted, cited context
ctx reindex [--gap MIN]       # rebuild the FTS index and the episodes from events
ctx timeline [--day today|yesterday|YYYY-MM-DD] [--project X --source S --since 7d --until DATE --tz Europe/Berlin --all --limit N --json]
ctx project list [--json]     # projects: allowed / local_only / mcp_exclude, sessions, events, last activity
ctx project show NAME [--json]
```

`--mode auto` (the default for `search`, `context` and MCP) is hybrid once embeddings exist
for the configured model (`$CTX_EMBED_MODEL`, default `model2vec:minishlab/potion-base-8M`)
and its embedder loads, otherwise FTS5; `vector`/`hybrid` fail with an actionable message
when the extra or the embeddings are missing. Set `CTX_EMBED_ON_SYNC=1` to have `ctx sync`
refresh embeddings afterwards (skipped with a note if the extra is not installed).
`ctx eval` scores retrieval against a question set; see `docs/CONTEXT_ENGINEERING.md` for
the file format and how to write a personal set (kept under `CTX_HOME`, never committed).

`ctx timeline` answers "what was I working on last Tuesday?": per local day and project,
the time span, sources, session titles, message / tool-call counts, and the top files
and commands (read from tool-call events). It spans all sources, shows only `allowed`
projects (`--all` adds the rest and unlinked web chats), and stores UTC but displays in
`--tz`, else `$CTX_TZ`, else the system zone. Bare dates in `--day/--since/--until` are
local dates. Episodes (same project, events < 45 min apart, all sources merged) are
derived and rebuilt on every `ctx sync` and by `ctx reindex`; no LLM is involved.

Plain `ctx sync` **skips** sources whose storage does not exist (shown as
`not installed`, exit 0) but still fails loud (exit 1) on a version-pin mismatch,
denylist violation or corrupt data in a source that is present. It prints one table
row per source: sessions/events added, duplicates, skipped records, unchanged
files/databases skipped by incremental state, and warnings. Unchanged JSONL files and
SQLite databases are not re-read; the state is derived and safe to delete.

### Web chat history (official exports)

ctx imports ChatGPT, Claude.ai and Gemini history from the data export each service
lets you download. No scraping, no browser access, no network.

```sh
ctx import chatgpt   ~/Downloads/<chatgpt-export>.zip   # or an extracted folder
ctx import claude-ai ~/Downloads/<claude-export>.zip
ctx import gemini    ~/Downloads/takeout-<...>.zip       # Takeout with My Activity > Gemini Apps
```

Or drop the zips into `<CTX_HOME>/imports/{chatgpt,claude_ai,gemini}/` and run plain
`ctx sync`. Zips are read in place (nothing is extracted); re-importing the same or a
newer export adds only new messages. Web chats are not linked to any project. Step-by-
step export instructions per service: `docs/CONNECTORS.md` sections 6a-6c.

### MCP server (use ctx as memory from any coding agent)

```
ctx mcp config --client claude-code   # prints `claude mcp add ctx -- ctx serve-mcp`
ctx mcp config --client codex         # also: opencode|cursor|gemini|hermes|generic
```

`ctx mcp config` only prints; it never edits your client's config. Snippets other than
Claude Code are marked UNVERIFIED. `ctx serve-mcp` runs a read-only stdio server with
tools `search`, `get_context`, `get_session`, `list_projects`, `recent_sessions`, `timeline`, and
`sync` (imports into ctx's own database only). Results are redacted, cited, and capped
at ~8k tokens. Hide a project from MCP with `mcp_exclude = true` in `projects.toml`;
disallowed projects (e.g. Home) are never served. See ADR-022.

Data lives in a per-user dir (override with `CTX_HOME`); project roots come from
`config/projects.toml` (or `<data dir>/projects.toml`).

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
| M1 — OpenCode import, search, `build_context` | implemented; prompt-history connector and `sources add` pending |
| M1.5 — Claude Code + Codex connectors | implemented (`ctx sync --source claude_code\|codex`); Codex unverified on real files |
| M1.6 — Hermes, Devin CLI, Antigravity CLI connectors | implemented (`ctx sync --source hermes\|devin\|antigravity_cli`); formats unofficial, checked on one real install; Antigravity protobuf conversations unreadable |
| M1.7 — Web export connectors (ChatGPT, Claude.ai, Gemini Takeout) | implemented (`ctx import <chatgpt\|claude-ai\|gemini> PATH`); synthetic fixtures only, unverified on real exports |
| M6 — MCP server (`ctx serve-mcp`) | implemented (pulled forward) |
| M2+ — timeline, handoff, synthesis | planned |

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
