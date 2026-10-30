# Decision log (ADRs)

Lightweight: date, decision, alternatives considered, reason. Add an entry for any
non-obvious choice. Newest at the bottom.

---

### 2026-10-11 · ADR-001 · Storage: single SQLite DB + FTS5 + blob files
Alternatives: vector DB server, per-source JSONL, Postgres. Reason: local-first,
zero-daemon, FTS5 is adequate for v1 keyword search; blobs keep oversized raw
content out of row updates. Embeddings deferred and, when added, live in a separate
rebuildable table.

### 2026-10-11 · ADR-002 · Import all, allow per-project
The 12 working directories observed in Phase 0 start `allowed=1` (explicitly
approved by the user); any project discovered later starts `allowed=0` until
enabled via `config/projects.toml` or CLI. Alternatives: strict allowlist import
(loses data), allow everything (privacy risk). Reason: nothing is lost at import,
features stay gated.

### 2026-10-11 · ADR-003 · Cloud LLM per-project opt-in
Every project starts `local_only=1`; cloud (NVIDIA OpenAI-compatible) only after
the user flips that project. Alternatives: cloud-for-all, fully-local-only.
Reason: privacy by default, still allows strong models where wanted.

### 2026-10-11 · ADR-004 · Giant tool outputs: spill > 64 KB to blobs
Content > 64 KB → content-addressed blob under `data/blobs/`, `events.text` keeps
head+tail (~4 KB each), `truncated=1`. Blob is written and fsynced **before** the
referencing event is inserted (no dangling refs). Motivated by real data: two
OpenCode `read` parts are 77 MB of inline JSON.

### 2026-10-11 · ADR-005 · Tooling: uv (via brew) + Python 3.11
Alternatives: pip+venv, pyenv-newer-Python. Reason: matches stated stack; uv 0.13.0
installed via Homebrew; system Python 3.11.9 already satisfies >=3.11.

### 2026-10-11 · ADR-006 · Local git repo, no remote
Reason: repo will contain personal-context-adjacent docs and fixtures; remote can
be added later deliberately.

### 2026-10-11 · ADR-007 · M1 sources: OpenCode only; core stays source-agnostic
OpenCode DB + prompt-history.jsonl are the reference connectors. Antigravity is
connector #2 (right after M1, findings documented now); zsh and VS Code later.
All source quirks live inside connector modules; core/common schema contains none.

### 2026-10-11 · ADR-008 · M2 = timeline (not handoff)
Deterministic first: episodes + `ctx timeline` need no LLM and validate linking.
Handoff follows.

### 2026-10-11 · ADR-009 · Project linking via cwd roots, not OpenCode project_id
Real data: 37/47 OpenCode sessions register under fallback project `/`; the true
working dir lives in `session.directory` / per-message `path.cwd` (12 distinct
dirs). Linking = longest `project_roots.root_path` prefix of the event cwd.
Alternatives: OpenCode project table (unreliable), git remote mapping (later).

### 2026-10-11 · ADR-010 · `config/projects.toml` overrides seeded projects
Users group directories into one project (e.g. SAR Images subtree → `sar-images`)
and toggle `allowed`/`local_only`. Loaded every sync; DB stays canonical.

### 2026-10-11 · ADR-011 · Connectors fail loud on schema/version drift
Each connector pins expected schema (tables + columns) and source version; mismatch
→ `ConnectorError` **before any writes**. Motivated by OpenCode having already
migrated JSON→SQLite once.

### 2026-10-11 · ADR-012 · Redaction invariants
Redact at ingestion (before `events`/FTS) **and** re-apply to any unredacted blob
content before display or LLM use. `ctx search --all` bypasses project-allow
filtering but **never** redaction. Unit-tested with fake secrets.

### 2026-10-11 · ADR-013 · Shared connector contract test suite
Every connector passes: idempotent re-sync, redaction-before-index, denylisted
paths/tables never touched, blob-before-event, `token_estimate` present. New
connectors (Antigravity etc.) inherit the guarantees for free.

### 2026-10-11 · ADR-014 · Source permission records + hard denylists in code
`sources` table stores enabled / allowed_paths / last_sync / schema_version_seen;
`ctx sources list | add | show` manages it. Denylists are declared in connector
code (not config) — OpenCode: tables `account`, `control_account`, `credential`,
`session_share`, `session_context_epoch`; files `auth.json`. The sync engine
re-checks before dispatch.

### 2026-10-11 · ADR-015 · Prompt-history timestamps approximated
`prompt-history.jsonl` carries no per-line timestamp; all 50 events use the file's
mtime, order preserved via line-number `source_id`. Accepted for 50 low-value
lines; revisit only if the file grows.

### 2026-10-11 · ADR-016 · Home catch-all project starts `allowed=0` (revises ADR-002)
Of the 12 Phase-0-observed directories, 11 start `allowed=1`; the Home catch-all
(`/Users/himanshu`) starts `allowed=0`. Anything matching only the Home root stays
out of search and LLM calls until the user enables specific folders in
`config/projects.toml`. Reason: Home matches every unfiled path, so allowing it by
default would silently expose unreviewed sessions. Supersedes the Home portion of
ADR-002.

### 2026-10-11 · ADR-017 · Redact then spill; blobs are redacted copies, not byte-exact
Pipeline order is parse → **redact** → truncate/spill: the full content written to
`data/blobs/` is the *redacted* copy of the source content, not a byte-exact raw
record. Alternatives: spill raw and re-redact at every use (secrets would persist
on disk; one missed re-redaction leaks), or truncate-then-redact (the blob would
hold the unredacted middle). Reason: ctx must never persist secrets anywhere;
byte-exact fidelity is sacrificed only where redaction matched. The
re-redaction-on-display invariant (ADR-012) remains as belt-and-braces.

### 2026-10-31 · ADR-018 · `events.message_id` added for citations
Search citations must name source session **and** message, but the original
`events` table only had `session_id`. Migration `0002_event_message_id` adds a
nullable `message_id` column and connectors set it via `RawEvent.message_ref`.
Alternatives: parse it out of `raw_json` (always NULL in v1), a side table
(extra join for every hit). Rows imported before the migration have NULL.

### 2026-10-31 · ADR-019 · Data dir moves to a per-OS user dir; `CTX_HOME` override
Default data dir is now `~/Library/Application Support/ctx` (macOS),
`$XDG_DATA_HOME/ctx` (Linux), `%LOCALAPPDATA%\ctx` (Windows) instead of
`<repo>/data`, so an installed `ctx` works from any directory and the repo
checkout never holds personal data. `CTX_HOME` overrides (tests); `CTX_DATA_DIR`
still works. Implemented by hand rather than adding `platformdirs`. Anyone with
an existing `<repo>/data` sets `CTX_HOME=<repo>/data`.

### 2026-10-31 · ADR-020 · Sync engine: events insert-only, sessions upserted, project via session
`INSERT OR IGNORE` on `UNIQUE(source, source_id)`: a changed upstream record never
rewrites an event (append-only, rule 1). Sessions are link metadata and are
upserted, so editing `projects.toml` re-links them; search resolves a hit's project
as `COALESCE(sessions.project_id, events.project_id)`, so re-linking needs no event
rewrite. Search shows only `allowed=1` projects unless `--all`; events with no
project are treated as not allowed (ADR-002/016). The engine re-redacts connector
output as defense in depth. Every sync is a full pass over a fresh snapshot
(incremental `time_updated` filtering is deferred; idempotency makes it safe).
