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
- Reads sources via a snapshot where the source is a live SQLite DB (OpenCode, Hermes,
  Devin, Antigravity CLI summaries). `connectors/sqlite_common.py` holds the WAL-safe
  open: `mode=ro` first (sees uncheckpointed `-wal` rows); only if that fails with
  CANTOPEN **and** no non-empty `-wal` exists, `immutable=1`; otherwise
  `ConnectorError` (never silently drop WAL rows).

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

## 2c. Hermes Agent (source name `hermes`)

Code: `src/ctx/connectors/hermes.py`. Unofficial/observed schema (Hermes 2026-10,
`schema_version` 32; shapes only, checked 2026-10-31 on a real install: 2 sessions,
109 messages).

| Path | What |
|---|---|
| `$HERMES_HOME/state.db` (default `~/.hermes`, Windows `%LOCALAPPDATA%\hermes`) | SQLite session store (**read**) |
| `$HERMES_HOME/profiles/<name>/state.db` | one DB per profile (**read**; no profiles exist on the dev machine, covered by tests only) |
| `.env`, `auth.json`, `auth.lock`, `config.yaml`, `.hermes_history`, `gateway_state.json`, `pairing/*`, `platforms/*`, `kanban.db*`, `projects.db*` | **denylisted** |

Precedence for the root: `CTX_SOURCE_ROOT_HERMES`, then `HERMES_HOME`, then the per-OS
default. Default source path is the root directory: the default DB plus every profile
DB are read. A single `state.db` file may also be passed.

DB: about 40 tables (WAL; the file had no `-wal`/`-shm` on disk, which makes plain
`mode=ro` fail with CANTOPEN, hence the fallback above). **Queried:** `sessions`,
`messages`, `schema_version`. **Denylisted (never queried):** all FTS shadow tables
(`messages_fts*`, `messages_fts_trigram*`), `state_meta`, `system_prompts`,
`gateway_*`, `hosted_room*`, `delivery_obligations`, `async_delegations`,
`session_admissions`, `session_model_usage`, `session_turn_leases`, `worker_*`, and
credential-ish names. Unselected columns on queried tables (`user_id`, `origin_json`,
`system_prompt`, billing fields, ...) are never read.

| Hermes | ctx |
|---|---|
| `sessions.id` | `source_session_id` (`<profile>/<id>` for non-default profiles) |
| `started_at`, `ended_at` / `last_activity_at` / last message (REAL epoch seconds) | `started_at`, `ended_at` (ms) |
| `cwd` | project linking (stored on 2/2 observed sessions; NULL -> unlinked) |
| `model`, `title` (redacted) | `model`, `title`; `agent = "hermes"` |
| `messages.timestamp` (REAL seconds) | `events.ts` (ms) |
| message `role` user/assistant | `events.role`; `system` -> role NULL, synthetic; `_compressed_summary=1` -> synthetic |
| `content` | kind `text` (empty skipped) |
| `reasoning_content` / `reasoning` | kind `reasoning` |
| `tool_calls` (OpenAI shape: `id`, `call_id`, `type`, `function{name, arguments(JSON string)}`) | one `tool:<name>` event each, `call_id`, text `input: <json>` |
| role `tool`, `tool_call_id` (58/58 match a call id) | kind `tool_result`, `call_id`; if the call's input named `.env`/`*.pem`/`*.key` the output is file-redacted |

`source_id = "<session ref>:<message_uid or id>:<block>"` (`block` = `text`,
`reasoning`, `call<i>`, `result`). Profile namespacing is required: integer
`messages.id` and session ids restart in every profile DB.
**Version pin** `hermes-sqlite-v1:schema-<n>`: required tables/columns present and
exactly one integer `schema_version` row, per DB; otherwise `ConnectorError`. Newer
`schema_version` numbers are accepted when the structure still matches (Hermes bumps
it often); structure, not the number, is the contract. Older per-session JSONL
storage is not supported.

## 2d. Devin CLI (source name `devin`)

Code: `src/ctx/connectors/devin.py`. Unofficial/observed (shapes only, checked
2026-10-31: 14 sessions, 3,579 `message_nodes` rows). Cloud Devin sessions are not
stored locally and are out of scope. The `cli-federal` variant named in Devin docs
has the same layout; point `CHISEL_SESSION_DB` or `CTX_SOURCE_ROOT_DEVIN` at it.

| Path | What |
|---|---|
| `~/.local/share/devin/cli/sessions.db` (`$XDG_DATA_HOME/devin`; Windows `%APPDATA%\devin\cli`) | SQLite (WAL) (**read**) |
| `cli/transcripts/<id>.json` (ATIF export, 13 of 14 sessions) | **not read**: the DB is the complete source |
| `credentials.toml`, `mcp/**` (OAuth), `oauth*`, `auth*.json`, `*token*`, `.env*` | **denylisted** |

Root precedence: `CTX_SOURCE_ROOT_DEVIN`, then `CHISEL_SESSION_DB` (a DB *file*; its
directory becomes the only allowed path), then the per-OS default. The default allowed
root is the whole `devin` directory so `credentials.toml` and `mcp/oauth` are
explicitly inside the denylist's reach.

Tables: **queried** `sessions(id, working_directory, model, agent_mode, created_at,
last_activity_at, title, ...)`, `message_nodes(session_id, node_id, parent_node_id,
chat_message JSON, created_at, metadata)`, `refinery_schema_history` (marker).
**Denylisted:** `tool_call_state` (duplicates what nodes carry), `prompt_history`,
`app_state`, `live_row_times`, `rendered_commits`, `subagent_heads`, `message_search*`.
Timestamps are INTEGER seconds; `chat_message.metadata.created_at` is ISO and
preferred.

`chat_message` keys: `message_id`, `role` (user/assistant/tool/system), `content`
(str), optional `thinking`, `tool_calls[{id, name, arguments, kind, index}]`,
`tool_call_id` (1158/1158 match a call id), `images`.

| Devin | ctx |
|---|---|
| `sessions.id` (a slug such as `word-word`) | `source_session_id`; `working_directory` -> project linking; `agent_mode` -> `agent` |
| `content` / `thinking` | kinds `text` / `reasoning`; system -> role NULL, synthetic |
| `tool_calls[]` | `tool:<name>`, `call_id = id`, text `input: <json>` |
| role `tool` | kind `tool_result` (role NULL), `call_id`; sensitive-file calls file-redact the output |
| `images` | kind `file`, text `[image]` (bytes dropped) |

**Quirk: node copies.** `message_nodes` is re-inserted on compaction/branching: 3,579
rows carry only 1,283 distinct `(session, message_id)`, and 95 of those groups differ
in content between copies. The first node (lowest `node_id`) wins and later copies are
counted as `duplicate_node` (events are immutable, rule 1). `source_id =
"<session>:<message_id>:<block>"`. Unparsable rows are counted; an unknown `role` is
skipped and counted.
**Version pin** `devin-sqlite-v1:refinery-<n>`: required tables/columns, and the first
200 nodes must include parsable JSON objects with `message_id` and `role`.

---

## 6. Web chat exports (ChatGPT, Claude.ai, Gemini) - source names `chatgpt`, `claude_ai`, `gemini`

Code: `src/ctx/connectors/{webexport_common,chatgpt_export,claude_ai_export,gemini_takeout}.py`.
Formats are the services' official data exports, reconstructed from public descriptions
and **only tested on synthetic real-shaped fixtures**; if a real export contradicts
this, stop and report. Exports are not browser history and ctx never touches a browser.

**Input.** `ctx import <chatgpt|claude-ai|gemini> PATH` where PATH is the downloaded
`.zip` or an extracted folder; the path becomes an allowed path for that run only.
Plain `ctx sync` scans `<CTX_HOME>/imports/<chatgpt|claude_ai|gemini>/` (override with
`CTX_SOURCE_ROOT_<NAME>`) for `*.zip` and extracted folders, newest first; a missing
directory is `not installed` (exit 0), an empty one imports nothing. Zips are read with
`zipfile` in memory, never extracted; zip members are size-capped (1 GiB). A `.zip` in
the watch dir with no recognised data is ignored with a warning; a corrupt zip, invalid
JSON or an unrecognised structure fails loud before anything is written.

**Common behavior.** Content is redacted, then oversized content spills to a blob.
Sessions have no `cwd`, so web chats are never linked to a project (no title/keyword
mapping, rule 7). Incremental cursor: size+mtime of the zip (or of the data files of an
extracted folder). Re-importing the same or a newer, larger export creates zero
duplicates: `source_id` derives from the conversation + message ids, and existing events
are never updated (an edited message in a later export keeps its first-imported text).
Members matching `denylist_paths` are refused before they are opened.

### 6a. ChatGPT (`chatgpt`, pin `chatgpt-export-v1`)

How to export: ChatGPT > profile menu > Settings > Data controls > Export data >
Confirm; open the emailed link and download the zip (the link expires after ~24 h).

- Reads only `conversations.json` or the shards `conversations-000.json`, ... (all
  shards; conversations repeated across shards are de-duplicated). Never read:
  `user.json`, `message_feedback.json`, `*account*.json`, `*auth*.json`; `chat.html`,
  `shared_conversations.json` and media are ignored.
- Each conversation's `mapping` is a tree. The **canonical branch** (`current_node` back
  to the root) is the thread. Other branches (regenerated answers, edited-away prompts)
  are **skipped** and counted as `alternate_branch_message`. Without a valid
  `current_node` the newest leaf is used (`no_current_node`). Parent cycles terminate.
- One event per message, `source_id = <conversation id>:<message id>`. user/assistant
  keep their role; system and tool messages have role NULL (system = `synthetic`).
  Hidden messages (`is_visually_hidden_from_conversation`) and empty ones are skipped.
- content_type mapping: `text`, `multimodal_text` (images -> `[image: WxH]`, audio ->
  `[audio]`, transcriptions kept) -> `text`; an assistant message addressed to a tool
  (`recipient` not `all`) -> `tool:<recipient>`; `code` -> `tool:code`/`tool:<recipient>`;
  `execution_output`, `tether_browsing_display`, `tether_quote`, `system_error`, any
  `tool` role -> `tool_result`; `thoughts`, `reasoning_recap` -> `reasoning`;
  `user_editable_context` / `model_editable_context` (profile and memory dumps) are
  skipped. Any other type is skipped and counted `unknown_content_type:<type>`.
- Session: id = conversation id, title, create/update time, model = last assistant
  `model_slug`.

### 6b. Claude.ai (`claude_ai`, pin `claude-ai-export-v1`)

How to export: claude.ai > profile menu > Settings > Privacy > Export data; open the
emailed link and download the zip.

- Reads `conversations.json` and (metadata only) `projects.json`. Never read:
  `users.json`, `*account*.json`, `*auth*.json`.
- One event per content block, `source_id = <conv uuid>:<message uuid>:<block index>`;
  `human` -> user, `assistant` -> assistant, other senders skipped. Blocks: `text`,
  `thinking` (-> `reasoning`), `tool_use` (-> `tool:<name>`), `tool_result`, `image` /
  `document` (placeholder `file`); unknown block types counted. If a message has no
  usable `content` blocks its `text` field is used. `attachments[].extracted_content`
  and `files[]` become `file` events (`att<i>`, `file<i>`).
- If a conversation's `project_uuid` is found in `projects.json`, the session `agent` is
  `project:<name>`. This is display metadata only; no ctx project is linked and project
  documents are not imported.

### 6c. Gemini (`gemini`, pin `gemini-takeout-v1`)

How to export: takeout.google.com > Deselect all > tick **My Activity** > "All activity
data included" > choose only **Gemini Apps** > format **JSON** (HTML also works) >
Create export, then download the zip.

- Reads only `.../Gemini Apps/MyActivity.json` (else `MyActivity.html`, parsed with
  the stdlib `html.parser`; English labels, i.e. the `Prompted` prefix). A folder named
  `Gemini Apps`, `Gemini` or `Bard` is required inside a Takeout zip, so other My Activity
  products (Search, YouTube) and the rest of the Takeout are never read; JSON entries
  whose `header`/`products` name another product are skipped.
- Each `Prompted ...` activity is a user event plus, if `safeHtmlItem` exists, an
  assistant event (HTML converted to text). Other activities (`Used Gemini Apps`, ...)
  are counted as `non_prompt_activity`. HTML times in `UTC`/`GMT+hh:mm` are converted;
  any other zone is treated as UTC with a warning.
- **Sessions are synthetic:** Takeout has no conversation ids, so activities sorted by
  time start a new session when the gap to the previous one exceeds 30 minutes
  (`SESSION_GAP_MS`). Session id = `g<first activity epoch ms>`; event id =
  `<sha256(time|prompt)[:16]>:prompt|response`. A superset export that bridges two
  earlier sessions makes new events join the merged session while stored events keep
  their old one.

---

## 3. Antigravity CLI (`agy`, source name `antigravity_cli`)

Code: `src/ctx/connectors/antigravity_cli.py`. **Not the Antigravity IDE.** Earlier
revisions of this section described both under one heading; they are different
storage trees: the CLI is `~/.gemini/antigravity-cli/` (this connector), the IDE is
`~/.gemini/antigravity/` (section 3a, documented only). Unofficial/observed, checked
2026-10-31 (shapes only).

| Path (under `~/.gemini/antigravity-cli/`) | What |
|---|---|
| `brain/<conversation-id>/.system_generated/logs/transcript_full.jsonl` | **read**: one step per line (44 files, 4,247 lines). `transcript.jsonl` is a truncated copy and is not read |
| `conversation_summaries.db` | **read** (snapshot): `conversation_summaries(conversation_id, title, workspace_uris JSON list of file:// URIs, agent_name, ...)`; 52 rows. `preview`, `raw_summary` are never selected |
| `cache/last_conversations.json` | **read**: `{workspace path: latest conversation id}`; project-link fallback |
| `conversations/*.db`, `*.pb` | **never opened**: protobuf blobs (`steps.step_payload`, `gen_metadata`, ...), no public schema. 5 of 49 conversations exist only here |
| `history.jsonl`, `settings.json`, `config.json`, `mcp_config.json`, `plugins/**/mcp_config.json`, `auth.json`, `oauth*`, `credentials*`, `installation_id`, `user_settings.pb`, `jetski_state.pbtxt`, `cli.log`, `log/*`, `crashes/*`, `plugin_data/*` | **denylisted** (`history.jsonl` is flagged as possibly secret-bearing and was not inspected) |
| `cache/conversation_metadata.json`, `knowledge/`, `annotations/`, markdown/PNG artifacts under `brain/` | not read (stale/duplicate; artifact import is future work) |

Root: `CTX_SOURCE_ROOT_ANTIGRAVITY_CLI`, else `~/.gemini/antigravity-cli`
(`%USERPROFILE%` on Windows). `AGY_CONVERSATIONS_DIR` relocates the `conversations/`
directory; it is only *listed* (file names) to count protobuf-only conversations, and
is added to `allowed_paths` when it lives elsewhere. The source path is the root
directory.

Step shape: `{step_index, type, source, status, created_at (ISO, seconds), content?,
thinking?, tool_calls?[{name, args}], input_tokens?, ...}`. Observed `source` values
`MODEL`, `USER_EXPLICIT`, `SYSTEM`; `status` `DONE`, `RUNNING`, `ERROR`, `INVALID`;
types `PLANNER_RESPONSE`, `USER_INPUT`, `RUN_COMMAND`, `VIEW_FILE`, `CODE_ACTION`,
`LIST_DIRECTORY`, `GREP_SEARCH`, `SEARCH_WEB`, `GENERIC`, `SYSTEM_MESSAGE`,
`CHECKPOINT`, `CONVERSATION_HISTORY` (no content), `ERROR_MESSAGE`, ...

| agy | ctx |
|---|---|
| `USER_INPUT` | kind `text`, role user |
| `PLANNER_RESPONSE` `thinking` / `content` | kinds `reasoning` / `text`, role assistant |
| `PLANNER_RESPONSE.tool_calls[]` | `tool:<name>`, text `input: <json>`; **no ids exist**, so `call_id = "<conversation>:<step>:call<i>"` |
| other `MODEL` steps (`VIEW_FILE`, `RUN_COMMAND`, ...) | kind `tool_result` (role NULL). Paired FIFO with the preceding planner calls (observed: always exactly one call per result step), taking its `call_id`; a `.env`/`*.pem` path in the paired call's args file-redacts the output |
| `SYSTEM` steps | role NULL, synthetic |
| `USER_EXPLICIT` non-input steps (user-run command) | `tool_result`, no call id |
| summaries `title` (redacted), `agent_name` | session `title`, `agent` (default `antigravity`) |
| summaries `workspace_uris[0]`, else `last_conversations.json` | `sessions.cwd` (project linking); neither -> unlinked |

`source_id = "<conversation>:<step_index>:<block>"`. A `RUNNING` step that is the
**last line** of a transcript is skipped (live; imported complete on a later sync);
the 90 observed `RUNNING` steps were all mid-file (stale), so those are imported.
Unknown `source` values and malformed lines are counted, never fatal.
**Version pin** `antigravity-cli-transcript-v1`: the head (200 lines) of every
transcript must be JSON objects with int `step_index`, `type`, `source`, parsable
`created_at`; `conversation_summaries` must keep its four columns. Protobuf-only
conversations are a **warning** (and dry-run counter `protobuf_only_conversation`),
not an error. Reverse-engineering `step_payload` is deferred and unverified.
`denylist_tables` lists the protobuf DB tables (never opened).

## 3a. Antigravity IDE (documented only, out of scope for now)

`~/.gemini/antigravity/` (top level: `brain/`, `code_tracker/`, `context_state/`,
`conversations/`, `implicit/`, `playground/`, `user_settings.pb`, `mcp_config.json`,
`browserAllowlist.txt`, `installation_id`). Same family of protobuf conversation
stores; **not parsed or imported**. When built it needs its own connector module and
the same denylist treatment (`mcp_config.json`, `plugins/**/mcp_config.json`,
`user_settings.pb`, `auth.json`). `~/.gemini/config/projects/*.json` (project name =
absolute path) is shared config and also unread.

## 4. VS Code (stub, later)

`~/Library/Application Support/Code/User/`: `History/` (453 file-history entries),
`workspaceStorage/` (64 workspaces, 752 MB), `globalStorage/state.vscdb` (434 KB,
key-value `ItemTable`). Candidate events: file save history + Copilot chat if
recoverable. Not inspected in depth.

## 5. Shell history (stub, later)

`~/.zsh_history`: 1,027 lines, zsh extended format. Low value per line; redaction
critical (commands contain tokens). Planned as `kind='shell'` events.
