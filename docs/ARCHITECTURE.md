# Architecture

## Overview

ctx is a pipeline: sources feed connectors, connectors feed an immutable raw-event
store, deterministic linking organizes events, and every feature (search, timeline,
Q&A, handoff, MCP) retrieves from that store. LLMs are optional consumers at the far
end — never collectors, never linkers.

```
 ┌─────────────┐   ┌──────────────┐
 │  OpenCode   │   │ prompt-history│  ... later: Antigravity, VS Code, zsh
 │  (SQLite)   │   │  (.jsonl)     │
 └──────┬──────┘   └──────┬───────┘
        │  connectors (isolated modules, version-guarded,
        │  denylist-enforced, redact-before-index)
        ▼
 ┌───────────────────────────────────────────┐
 │ RAW EVENTS  (SQLite, append-only)         │   + blobs on disk (data/blobs)
 │ events · sessions · projects · sources    │
 └──────┬──────────────────────┬─────────────┘
        │                      │
        ▼                      ▼
 ┌──────────────┐    ┌─────────────────────┐
 │ SEARCH INDEX │    │ SYNTHESIZED MEMORY  │
 │ events_fts   │    │ episodes · facts    │
 │ (rebuildable)│    │ (derived, evidence- │
 └──────┬───────┘    │  linked, rebuildable│
        │            └──────────┬──────────┘
        └──────────┬────────────┘
                   ▼
        ┌─────────────────────┐
        │ build_context()     │   ← the ONLY prompt-assembly path
        │ (budgeted selection)│
        └──────┬──────────────┘
               ▼
    ctx search · ctx timeline · ctx ask · ctx handoff · ctx serve-mcp
```

## Layers

1. **Raw events** — immutable, append-only. One row per source artifact (message
   part, tool call result, prompt). Full oversized content lives in content-addressed
   blob files. Never edited, never deleted (except explicit user purge of `data/`).
2. **Searchable index** — FTS5 virtual table over `events.text`. Fully rebuildable
   from `events` alone (`ctx reindex`). No source of truth lives here.
3. **Synthesized memory** — episodes (deterministic time-bucketed groupings) and
   facts (LLM-extracted, later). Every fact links to `evidence_event_ids` and carries
   status `suggested | confirmed | abandoned`. Dropped and rebuilt from raw events at
   any time.
4. **Relationships** — deterministic only: project roots (longest-prefix cwd match),
   sessions (native source sessions), episodes (same project + idle-gap threshold).
   **An LLM never decides what belongs together.**

## Data flow & invariants

Sync pipeline per source:
`snapshot source → parse → redact → truncate/spill → write blob (fsync) → upsert event → (re)index`

Invariants (enforced in code, tested by the connector contract suite):

1. **Idempotent ingestion.** `UNIQUE(source, source_id)`; re-running a sync creates
   zero duplicates. `content_hash` detects changed records.
2. **Redact before index.** Text passes the redactor before it reaches `events.text`
   or FTS. Any unredacted content (blob) is re-redacted before display or LLM use.
   `ctx search --all` bypasses project allow-filtering, **never** redaction.
3. **Blob before event.** A blob file is written and fsynced *before* the event row
   that references it is inserted. An orphan blob is harmless; a dangling
   `blob_ref` is a bug.
4. **Size-aware reads.** Connectors never blindly load oversized fields (OpenCode
   has 77 MB tool parts); they check sizes and stream/spill.
5. **Fail loud on drift.** Each connector pins a schema/version; on mismatch it
   aborts **before any writes**, with a clear error.
6. **Truncate, don't lose.** Oversized text is stored head+tail (~8 KB total) in
   `events.text`, full content in `blob_ref`, `truncated=1`, `token_estimate` always set.

## Connector contract

Every connector lives in its own module under `src/ctx/connectors/` behind one
interface. The core engine contains no source-specific logic.

```python
class Connector(Protocol):
    name: str                          # "opencode", "opencode_prompts", ...
    schema_version_pin: str            # fail loud if source version is unrecognized
    allowed_paths: list[Path]          # ONLY these roots may be read
    denylist_paths: list[str]          # path patterns never read (e.g. "**/auth.json")
    denylist_tables: list[str]         # DB tables never queried (e.g. "credential")

    def check_version(self, source_path: Path) -> str: ...   # raises ConnectorError
    def fetch_changes(self, state: SyncState) -> Iterator[RawEvent]: ...
```

- **Source permission records** (table `sources`): `enabled`, `allowed_paths`,
  `schema_version_seen`, `last_sync`. Managed via `ctx sources list | add | show`.
- **Hard denylists are code, not config.** A connector physically cannot read its
  denylisted tables/paths; the sync engine re-checks before dispatch.
  OpenCode's denylist: tables `account`, `control_account`, `credential`,
  `session_share`, `session_context_epoch`; files `auth.json`.
- **Shared contract test suite** (`tests/connector_contract.py`): every connector
  must pass, on a real-format fixture: (a) idempotent re-sync → zero duplicates;
  (b) redaction applied before indexing; (c) denylisted paths/tables never touched;
  (d) blob written before referencing event; (e) every event has `token_estimate`.
- **Sync engine** (`src/ctx/core/sync.py`) is source-agnostic: it re-checks
  `allowed_paths`/`denylist_paths` before dispatch, calls `check_version` before any
  write, re-redacts every event, verifies referenced blobs exist, links projects by
  longest cwd prefix, and inserts with `INSERT OR IGNORE` (events are never updated).
  `ctx sync --dry-run` writes nothing. Connectors are registered in
  `src/ctx/connectors/registry.py`.
- The contract suite is a reusable class (`ConnectorContract`); a connector's test
  module subclasses it and supplies fixtures (see `tests/test_opencode_contract.py`).
- Details per source: `docs/CONNECTORS.md`.

## Privacy model

- **Local-first.** DB, blobs, indexes stay in the per-user data dir (`CTX_HOME`
  override; see ADR-019) or `./data/` in a dev checkout (gitignored). Nothing
  leaves the machine in M1; later, LLM calls leave only via the provider
  abstraction, only for projects with `local_only=0`.
- **Per-project allow.** `projects.allowed` gates search/feature visibility. 11 of
  the 12 seeded Phase-0 dirs start `allowed=1`; the Home catch-all and anything
  discovered later start `allowed=0` (ADR-002, ADR-016).
- **Redaction.** Regex-based redactor applied at ingestion *and* on any blob
  content at render time. Fake-secrets unit tests are part of M1 acceptance.
  Typed placeholders `[REDACTED:<kind>]`; redaction is idempotent. Patterns
  (implemented in `src/ctx/core/redact.py`):

  | Kind | Catches |
  |---|---|
  | `secret` | key/value assignments: `api_key`, `secret`, `token`, `access/refresh/auth token`, `client_secret`, `private_key`, `password` … followed by `=`/`:` and a ≥8-char value (JSON, YAML, `.env`, shell styles) |
  | `api-key` | OpenAI-style `sk-…` (runs after `anthropic-key` so `sk-ant-…` types correctly) |
  | `anthropic-key` | `sk-ant-…` |
  | `stripe-key` | `sk_live_/rk_live_…` (and `sk_test_/rk_test_`) |
  | `npm-token` | `npm_…` |
  | `huggingface-token` | `hf_…` |
  | `nvapi-key` | NVIDIA `nvapi-…` |
  | `github-token` | `ghp_/gho_/ghu_/ghs_/ghr_…` |
  | `aws-access-key` | `AKIA…` |
  | `google-api-key` | `AIza…` |
  | `slack-token` | `xox[baprs]-…` |
  | `auth-header` | `Authorization: Basic/Bearer …` headers |
  | `bearer-token` | standalone `Bearer …` |
  | `jwt` | `eyJ…….` three-segment JWTs |
  | `private-key` | `-----BEGIN … PRIVATE KEY-----` blocks (match window bounded) |
  | `url-password` | `scheme://user:password@host` credentials |

  File-aware redaction: for files named `.env`, `.env.*`, `credentials`, `*.pem`,
  `*.key` the connector applies `redact_file_text(path, text)` — every key/value
  line is redacted (key names are not inspected), and `*.pem`/`*.key` contents are
  replaced wholesale. This closes the generic key-name limitation for files whose
  names mark them sensitive.

  Known limitation: `.env`-style values under non-secret-looking keys in files
  *not* matching the sensitive names above are not recognized generically.
- **local_only.** `LocalProvider` (Ollama) for `local_only=1` projects;
  `OpenAICompatibleProvider` (NVIDIA) only for opted-in projects (ADR-003).

## Context assembly seam (build now, intelligence later)

Any feature that sends context to an LLM (`ask`, `handoff`, MCP) must go through
**one** function — no other code path may assemble prompts:

```python
build_context(conn, query: str, budget_tokens: int, project: str | None = None, *,
              include_all: bool = False, top_n: int = 50) -> ContextPackage
```

`ContextPackage` carries: selected items (each with text, type,
`evidence_event_ids`, citation), total token count used, and what was left out
(`left_out`: event id, tokens, reason). Implemented in `src/ctx/core/context.py`;
the best hit is trimmed head+tail to fit when nothing else would, and text is
re-redacted before it is returned. `conn` is the DB handle the seam reads from.

v1 behavior is deliberately dumb: top-N FTS5 hits trimmed to budget using
`token_estimate`. Pruning, compaction, reranking, rolling summaries, and budget
allocation are deferred (`docs/CONTEXT_ENGINEERING.md`) and will swap in behind
this interface without touching callers. Evidence IDs are always returned so detail
can be fetched on demand.
