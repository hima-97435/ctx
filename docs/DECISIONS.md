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

### 2026-10-31 · ADR-021 · Claude Code + Codex connectors (M1.5); roadmap order
Adds sources `claude_code` and `codex` on the existing contract, before further
roadmap items: the user's own coding-agent transcripts are the highest-value, lowest-risk
data (plain append-only JSONL, no live-DB snapshot). Roadmap: new M1.5; MCP server (M6)
stays ahead of Antigravity (M7); web chat exports are planned (M8, not started); Hermes
and Gemini CLI follow in the same family. Choices: (1) separate `tool_result` kind
rather than merging output into the call (these formats store call and result as
separate records); (2) sidechain lines and bookkeeping/unknown line types are counted
and skipped, never fatal, unknown *shape* of message records is fatal (ADR-011);
(3) `jsonl_common.py` holds source-neutral helpers (line reading, redact-then-spill,
stats) so each connector file stays source-specific; (4) `.zst` rollouts need the
optional `zstandard` package, not a hard dependency; (5) `denylist_tables` is empty
(file sources) and the contract suite's SQL-trace test is switched off via
`expects_sql = False`; (6) incremental sync stays a full pass with idempotent inserts
(ADR-020) - a size/mtime cache needs an engine state seam and is deferred. Codex shape
is from public documentation only (no local rollouts to check); documented as unverified.

### 2026-10-31 · ADR-022 · MCP server: read-only facade, `mcp_exclude` opt-out
`ctx serve-mcp` lets the user's chosen coding agents query ctx. Decisions: (1)
`local_only` governs ctx's *own* LLM calls (ADR-003); MCP instead returns redacted
data to an agent the user configured, so it is not gated by `local_only`. (2) The
opt-out is a new per-project `mcp_exclude` (config -> `projects.mcp_exclude`,
migration 0003); excluded projects are never returned and look unknown if named. (3)
`allowed=0` projects, including Home (ADR-016), and unfiled events stay invisible; no
tool argument can widen visibility (no `include_all`). (4) Only `build_context`
assembles context (rule 8); all output is re-redacted (rule 4) and capped at 8k tokens.
(5) `sync` is exposed because it is idempotent and writes only ctx's own data dir; it
reads source storage read-only as `ctx sync` does. (6) `ctx mcp config` prints
snippets and never writes client config (rule 10); only Claude Code's is verified.
(7) Uses `mcp` 2.x `MCPServer` (renamed from v1 `FastMCP`). Roadmap: M6 pulled ahead of
M2; timeline tool deferred.

### 2026-10-31 · ADR-023 · Hermes, Devin CLI and Antigravity CLI connectors (M1.6)
Three more sources on the existing contract, observed on one real install each
(formats unofficial; shapes in `docs/CONNECTORS.md`). Choices: (1) **WAL-safe open**
(`sqlite_common.py`): `mode=ro` first because it sees uncheckpointed WAL rows;
`immutable=1` only when `mode=ro` cannot open the file and no non-empty `-wal` exists,
else fail loud. A real Hermes DB (WAL mode, no `-wal`/`-shm` on disk) cannot be
opened `mode=ro`; `immutable=1` on the live Devin DB silently lost 27 of 3,579 rows,
which is why it is never a blanket choice. (2) **Hermes profiles** are namespaced
(`<profile>/<id>`) in both session and event ids, since integer message ids restart
per DB; sessions of every DB are yielded before any event. (3) **Devin** `message_nodes`
carries ~2.8 copies per `message_id` (compaction/branching); first node wins, because
events are immutable; the `transcripts/*.json` export is not read (partial, redundant)
and `tool_call_state` is denylisted (duplicates node content). (4) **Antigravity**:
only the CLI (`~/.gemini/antigravity-cli`) is implemented; the IDE is a different tree
and stays documented-only (CONNECTORS section 3 was split accordingly). The protobuf
conversation DBs are never opened (no public schema); the plain-JSONL
`transcript_full.jsonl` is the source, `conversation_summaries.db` +
`last_conversations.json` give titles and project linking. Transcripts have no tool
call ids, so results are paired FIFO with the preceding planner calls (always 1:1 in
observed data) to carry a synthetic `call_id` and apply `.env` file-aware redaction.
A `RUNNING` step is skipped only as the last line of a file. (5) Project linking uses
only stored cwd/workspace values (rule 7); Hermes sessions without `cwd` stay
unlinked. (6) `ctx sync` with no `--source` now reports an error for each source whose
storage is not installed (same as Codex before); making absence a `skipped` status is
left as a roadmap item. `ctx mcp config --client hermes` already existed (unverified).

### 2026-10-31 · ADR-024 · Absent sources, incremental sync state, status and doctor
Resolves the ADR-023 roadmap item. Choices: (1) **Absent storage is a skip, not an
error**, but only for implicit selection: plain `ctx sync` marks a source whose path
does not exist `not_installed` (exit 0); `ctx sync --source X` on it is an error
(exit 1). The access check (allowed paths + denylist) runs *before* the existence
check, so a denylisted path still fails loud, and anything that exists keeps failing
loud on version-pin mismatch or corrupt data. (2) **`sync_state` table** (migration
0004), keyed `(source, key)` with a JSON value, written by the engine in the same
transaction as the events; connectors only `stage` cursors. It is derived, so
deleting it is always safe (full rescan, `INSERT OR IGNORE`, zero duplicates);
unparsable values count as absent. (3) **Cursor = `[size, mtime_ns]` per JSONL file
and per SQLite DB (plus its `-wal`)**, taken before reading. Any change re-reads the
whole unit, so truncation/rewrite/same-size edits are all caught. A max-rowid
watermark was rejected for filtering: file-aware redaction pairs a tool result with
its earlier tool call, and reading only new rows would lose that pairing (a
redaction regression, rule 4). Antigravity always re-yields sessions (titles/cwd come
from side files) and skips only unchanged transcripts' events. (4) **Session upsert
merges** (`coalesce` for cwd/model/agent/title, max `ended_at`) and events link to a
session already stored by an earlier run, because incremental scans see only part of a
session. Project links are re-derived from stored `cwd` on every sync (config edits
must take effect without re-reading sources). (5) The engine passes `state` only to
connectors whose `fetch` accepts it, so older connectors keep working. (6)
`ctx status` and `ctx doctor` open the DB `mode=ro`: they never create or migrate it.
Doctor's FTS check compares `events` with `events_fts_docsize` (an external-content
FTS `count(*)` just reads `events`); fix is `ctx reindex`. (7) `ctx sync` prints a
per-source table (sessions+, events+, duplicates, skipped, unchanged, warnings,
schema) replacing the one-line-per-source format.

### 2026-10-31 · ADR-025 · Web chat history through official data exports
Revises the VISION non-goal ("no full browser history"): that stays true, and **exports
are not browser history**. The user downloads the services' own data export (ChatGPT,
Claude.ai, Google Takeout > Gemini Apps) and ctx imports the file; no scraping, no
browser access, no network. A browser extension for live capture is deferred (roadmap
M1.7), not rejected; it needs its own threat model. Choices: (1) Connectors `chatgpt`,
`claude_ai`, `gemini` share `webexport_common.py` (source-neutral archive and denylist
handling, rule 6). (2) Zips are read in memory with `zipfile`, never extracted;
denylisted members (`user.json`, `users.json`, `message_feedback.json`, account/auth
files) are refused before being opened, and the Gemini connector selects members by path
so other Takeout products are not read. (3) `allowed_paths` is the watch dir
`<CTX_HOME>/imports/<source>/`; `ctx import` adds the explicit user-supplied path as an
extra allowed path for that run (the alternative, copying the zip into `imports/`,
doubles disk use and keeps sensitive archive members on disk). (4) ChatGPT: import the
canonical branch only; alternates are skipped and counted. They are regenerated/edited-
away drafts that would pollute search; nothing is lost, the export still has them, and
`events` stays append-only so a later canonical-branch change adds events and never
rewrites old ones. (5) Edited messages in a later export keep their first-imported text
(`INSERT OR IGNORE`, rule 1). (6) Gemini sessions are synthesized by a 30-minute gap rule
because Takeout has no conversation ids; ids derive from time + prompt text. (7) Web
chats are never project-linked (no cwd; title/keyword mapping rejected as non-
deterministic guessing, rule 7). Claude.ai `projects.json` is surfaced only as
`agent=project:<name>`. (8) Newest archive is processed first so its session metadata
wins; unchanged archives are skipped via the size+mtime cursor (ADR-024). (9) Formats
are reconstructed from public descriptions and verified only on synthetic fixtures.
