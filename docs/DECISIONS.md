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
