# Connectors

Connectors turn source-specific storage into common-schema events. Findings below
come from a read-only Phase 0 inspection (2026-10-11, macOS 26.5.2, OpenCode 1.18.34).

## Contract (applies to every connector)

- One module per source under `src/ctx/connectors/`; core engine has zero
  source-specific logic.
- Declares: `name`, `schema_version_pin`, `allowed_paths`, `denylist_paths`,
  `denylist_tables`.
- **Fails loudly** (raises `ConnectorError`, aborts before any writes) when the
  source's schema/version doesn't match the pin (addition #5).
- Reads only inside `allowed_paths`; never touches denylisted tables/files —
  enforced in code and re-checked by the sync engine (addition #2).
- Must pass the shared contract suite (`tests/connector_contract.py`): idempotent
  re-sync; redaction before indexing; denylist untouched; blob-before-event;
  `token_estimate` on every event (addition #3).
- Reads sources via a snapshot where the source is a live SQLite DB (OpenCode).

---

## 1. OpenCode (reference connector, M1)

### Storage location & format

| Path | What | Size (observed) |
|---|---|---|
| `~/.local/share/opencode/opencode.db` | SQLite (WAL), tables `session`, `message`, `part`, `todo` + others | 208 MB, live |
| `~/.local/share/opencode/storage/` | legacy JSON store (session_diff, migration) | 676 KB, mostly empty |
| `~/.local/share/opencode/tool-output/` | spilled tool outputs | 1 file |
| `~/.local/state/opencode/prompt-history.jsonl` | one JSON per typed prompt | 50 lines |
| `~/.local/share/opencode/auth.json` | **credentials — denylisted** | — |

Observed data: 47 sessions / 1,618 messages / 6,093 parts, 2026-04-24 → 2026-10-10.

### DB schema (as observed)

- `session(id, project_id, parent_id, slug, directory, title, version, agent,
  model JSON {"providerID","modelID"}, cost, tokens_input/output/reasoning/cache_*,
  time_created*, time_updated*, time_archived, workspace_id, metadata, …)`
  (*epoch ms*)
- `message(id, session_id, time_created, time_updated, data JSON)` — `data` holds
  `role` (`user`|`assistant`), `path {cwd, root}`, `model`, `tokens`, `cost`,
  `parentID`, `summary.diffs`. Messages are **metadata**; content lives in parts.
- `part(id, message_id, session_id, time_created, time_updated, data JSON)` —
  `data.type` ∈ `text | reasoning | tool | patch | file | step-start | step-finish |
  compaction`; tools carry `state {status, input, output, metadata, title, error}`,
  plus `callID`, `filename`, `mime`, `url`, `synthetic`, `hash`, `files`.
- Tool usage observed: read 474, bash 416, edit 149, grep 82, glob 80, write 50,
  todowrite 24, question 10.
- **Never queried (denylist)**: `account`, `control_account`, `credential`,
  `session_share`, `session_context_epoch`, `event` (internal pub/sub replay log),
  empty tables `session_message`, `session_input`, `workspace`. File `auth.json`.

### Field mapping → common schema

| OpenCode | ctx |
|---|---|
| `session` | `sessions` row (`source_session_id=id`, `cwd=directory`, `model`, `agent`, `title`, `started_at=time_created`, `ended_at=time_updated`) |
| message `role` | `events.role` |
| part `type` (tool parts: `tool:<name>`) | `events.kind` |
| part `callID` | `events.call_id` |
| part `synthetic` | `events.synthetic` |
| part content (below) | `events.text` (redacted, truncated) |
| message `path.cwd` / session `directory` | project linking (longest root prefix) |
| part `id` | `events.source_id` |
| part `message_id` | `events.message_id` (citations) |
| part `time_created` (ms) | `events.ts` |

Part content extraction: `text`→`text`; `reasoning`→`text`; `tool`→ `state.output`
(or `state.input` when no output: commands, queries); `patch`→ file list + hash;
`file`→ filename + mime (content stays in blob); `compaction`→ marker only,
`synthetic=1`; `step-start/step-finish` → skipped (no content).

### Known quirks

1. **Giant inline tool parts** — two `read` outputs are 77 MB of inline JSON;
   19 parts > 100 KB. Size-aware reads mandatory; > 64 KB spills to blob
   (head+tail kept in `text`).
2. **Project table unreliable** — 37/47 sessions registered under fallback project
   `/`; real cwd lives in `session.directory` / message `path.cwd` (12 distinct
   dirs). We never link via OpenCode's `project_id`.
3. **Live WAL DB** — OpenCode may be running during sync; the connector syncs from
   a snapshot copy (SQLite backup API into `data/tmp/`), never the live file.
4. **Legacy + modern storage coexist** — `storage/` JSON is a dead end (post-migration);
   the DB is the source. `tool-output/` files are re-attached via part fields if needed.
5. **Version pin** — pin: presence of tables {`session`,`message`,`part`,`todo`,
   `project`} with observed columns + `__drizzle_migrations` non-empty. Anything
   else → `ConnectorError` before writes.
6. **Epoch milliseconds** everywhere; roles only user/assistant; 6 `synthetic` text
   parts exist (flagged, kept, excluded from later synthesis).
7. **Compaction markers** — `compaction` parts with `tail_start_id` record where
   OpenCode compacted context; imported as markers (useful provenance later).

### Incremental sync

State: `sources.last_sync` + max `events.ts` per source + snapshot of seen
`content_hash` for changed rows. Full pass reads snapshot DB; rows with
`time_updated > last_sync` (sessions) / `time_updated > last_sync` (messages, parts)
are upserted; `UNIQUE(source, source_id)` guarantees idempotency. First sync is a
full backfill (~6k parts, seconds).

---

## 2. OpenCode prompt history (M1, same repo, separate source name)

`~/.local/state/opencode/prompt-history.jsonl`: 50 lines, keys `{input, mode, parts}`.
- Map each line → one event: `kind='prompt'`, `role='user'`, `text=input`.
- **No timestamp in the file** → use file mtime as `ts` for all lines (documented
  limitation; ordering within the file is preserved by line number in `source_id`).
- No session linkage (`session_id` NULL); project resolution falls back to `home`
  root — acceptable for 50 lines.

---

## 2a. Claude Code (M1.5, source name `claude_code`)

Code: `src/ctx/connectors/claude_code.py` (shared line/redaction helpers in
`jsonl_common.py`). Format is **unofficial**; shapes below were checked against a
real install (key names/types only, 2026-10-31, Claude Code 2.1.x).

| Path | What |
|---|---|
| `~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl` | append-only transcript, one JSON per line (**the only thing read**) |
| `~/.claude/.credentials.json`, `~/.claude.json`, `settings*.json`, `config.json`, `history.jsonl`, `.env*` | **denylisted**, never opened |

Allowed path: `~/.claude` (override root with `CTX_SOURCE_ROOT_CLAUDE_CODE`);
default source path `~/.claude/projects`. Only `projects/*/*.jsonl` is globbed.
Subagent files under `<session>/subagents/` are not read.

**Observed line types** (counted in the dry-run report): `user`, `assistant`
(imported) plus bookkeeping that is counted and skipped: `mode`, `permission-mode`,
`atis-latch`, `last-prompt`, `ai-title` (used as session title), `tools-used`,
`queue-operation`, `cost-state`, `file-history-snapshot/-delta`, `attachment`,
`system`, `summary`, `api-request`, `api-request-blob`, `api-request-shape`. Any
other `type` is counted as `unknown_type:<t>` and skipped. Message records carry
`uuid, parentUuid, sessionId, timestamp (ISO), cwd, gitBranch, version,
isSidechain, message{role, model, content}`. `content` is a string or blocks:
`text`, `thinking` (mostly signature-only, empty text -> skipped), `tool_use{id,name,input}`,
`tool_result{tool_use_id,content,is_error}` (content str or text blocks), `image`,
`server_tool_use`, `advisor_tool_result` (skipped, counted as unknown block).

| Claude Code | ctx |
|---|---|
| `sessionId` | `sessions.source_session_id`; `cwd` (first seen) -> project linking; `message.model` (last assistant); `ai-title` -> title; min/max `timestamp` -> started/ended |
| line `uuid` | `events.message_id`; `events.source_id = "<uuid>:<block index>"` |
| `timestamp` | `events.ts` (epoch ms) |
| `text` / `thinking` / `image` | kinds `text` / `reasoning` / `file` (`[image: mime]`, data dropped) |
| `tool_use` | `tool:<name>`, text = `input: <json>`, `call_id = id` |
| `tool_result` | kind `tool_result`, `call_id = tool_use_id`, role `user`; if the call's input named `.env`/`*.pem`/`*.key`, whole output is file-redacted |
| `isMeta`, `isCompactSummary` | `synthetic=1` |
| `isSidechain=true` | skipped, counted |

Quirks: resumed sessions copy earlier lines into a new file (same `uuid`) -> deduped
in-run, and by `UNIQUE(source, source_id)` across runs; the last line of a live
transcript may be torn (counted as malformed, picked up complete on the next sync);
a `.env` read result with a known call id is redacted by file name. Large tool
results (>64 KB) are redacted, then spilled to a blob.

**Version pin** `claude-code-jsonl-v1[:cli-<version>]`: every `user`/`assistant`
record in the head (200 lines) of every file must have `uuid`, `sessionId`, a
parsable `timestamp` and `message.content`, lines must be JSON objects. Anything
else -> `ConnectorError` before any write.

**Incremental:** every sync is a full pass (ADR-020). An appended transcript adds
only the new events (tested); a size/mtime skip-cache is deferred until the engine
has a per-source state seam.

## 2b. Codex CLI (M1.5, source name `codex`)

Code: `src/ctx/connectors/codex.py`. **Not verified against real files**: no
`~/.codex/sessions` exists on the dev machine (this Codex version keeps state in
`~/.codex/*.sqlite`, which is denylisted and not read). Mapping follows the
documented rollout format; verify with `python -m ctx.connectors.codex` on a
machine that has rollouts.

| Path | What |
|---|---|
| `~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<id>.jsonl` | one JSON per line `{timestamp, type, payload}` (**read**) |
| `...jsonl.zst` | read only if the optional `zstandard` package is installed; else skipped with a warning (counted `zst_without_zstandard`) |
| `~/.codex/auth.json`, `config.toml`, `config.json`, `installation_id`, `*.sqlite*` | **denylisted** |

Allowed path `~/.codex` (override `CTX_SOURCE_ROOT_CODEX`); default source path
`~/.codex/sessions`. Line 1 must be `session_meta{id, cwd, timestamp, originator,
cli_version, instructions}` (`instructions` is never stored). `turn_context` supplies
the model. `response_item` payloads are mapped; `event_msg` (duplicates), `turn_context`
and unknown record/item types are counted and skipped.

| Codex `response_item` | ctx |
|---|---|
| `message` (content `input_text`/`output_text`) | kind `text`; role user/assistant; `developer`/`system` -> role NULL, synthetic; `<environment_context>`, `<user_instructions>`, `# AGENTS.md instructions` -> synthetic |
| `reasoning` | kind `reasoning` from `summary` (encrypted-only -> skipped) |
| `function_call`, `custom_tool_call`, `local_shell_call` | `tool:<name>` (`local_shell`), `call_id` |
| `function_call_output`, `custom_tool_call_output` | kind `tool_result`, `call_id`; sensitive-file reads (e.g. `cat .env`) file-redacted |

`source_id = "<session id>:<line number>"` (items have no ids; files are
append-only), `session.cwd = session_meta.cwd`.
**Version pin** `codex-rollout-v1[:cli-<version>]`: the first line of every rollout
must be a `session_meta` with a string `id`; older headerless rollouts or any other
shape -> `ConnectorError` before writes.

---

## 3. Antigravity (documented only — connector #2, after M1)

Read-only Phase 0 inspection; **not parsed or imported yet**.

| Path | What | Size |
|---|---|---|
| `~/.gemini/antigravity-cli/conversations/*.db` | 39 SQLite DBs, content in protobuf BLOBs (`steps.step_payload`, `metadata`, `render_info`) | 166 MB |
| `~/.gemini/antigravity-cli/conversations/*.pb` | 3 protobuf dumps | ≤ 10 MB each |
| `~/.gemini/antigravity-cli/conversation_summaries.db` | plain-text metadata: `title, preview, step_count, last_modified_time, workspace_uris, project_id, agent_name, status` | 128 KB |
| `~/.gemini/antigravity-cli/brain/<uuid>/` | 1,504 plain files: markdown reports, PNGs, `*.metadata.json` | 140 MB |
| `~/.gemini/antigravity-cli/history.jsonl` | session history index (pattern-matched as possibly secret-bearing; contents not yet inspected) | 228 KB |
| `~/.gemini/config/projects/*.json` | project name = absolute path | small |

Plan when built: `conversation_summaries.db` as the index (titles, workspaces,
timestamps → sessions), `brain/` markdown artifacts as file-kind events, then
reverse-engineer `steps.step_payload` protobuf (biggest effort, best evidence).

**Denylist (explicit, to be enforced in code when this connector is built):**

- `mcp_config.json` (e.g. `~/.gemini/config/mcp_config.json`)
- `plugins/**/mcp_config.json` (e.g. `~/.gemini/config/plugins/mem0/mcp_config.json`)
- `history.jsonl` (`~/.gemini/antigravity-cli/history.jsonl` — pattern-matched as
  possibly secret-bearing; contents not yet inspected)
- `auth.json` (any credential file of that name under the source root)

## 4. VS Code (stub, later)

`~/Library/Application Support/Code/User/`: `History/` (453 file-history entries),
`workspaceStorage/` (64 workspaces, 752 MB), `globalStorage/state.vscdb` (434 KB,
key-value `ItemTable`). Candidate events: file save history + Copilot chat if
recoverable. Not inspected in depth.

## 5. Shell history (stub, later)

`~/.zsh_history`: 1,027 lines, zsh extended format. Low value per line; redaction
critical (commands contain tokens). Planned as `kind='shell'` events.
