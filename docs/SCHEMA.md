# Schema

SQLite (WAL mode), one database at `data/ctx.db`, plus content-addressed blobs under
`data/blobs/`. Timestamps are **epoch milliseconds** (source-native for OpenCode).
`token_estimate` is computed at import by one isolated function (v1: `len(text)//4`)
so a real tokenizer can replace it later.

Design rule: **`events` is the only source of truth.** Sessions link events; the FTS
index, episodes, facts, and embeddings are derived and rebuildable.

## Sources & projects (linking + permissions)

```sql
CREATE TABLE sources (
  id                  INTEGER PRIMARY KEY,
  name                TEXT NOT NULL UNIQUE,     -- 'opencode', 'opencode_prompts'
  enabled             INTEGER NOT NULL DEFAULT 1,
  allowed_paths       TEXT NOT NULL DEFAULT '[]', -- JSON array: roots the connector may read
  schema_version_seen TEXT,                     -- last version accepted by the connector
  last_sync           TEXT,                     -- ISO-8601 UTC
  created_at          TEXT NOT NULL
);

CREATE TABLE projects (
  id         TEXT PRIMARY KEY,                  -- slug, e.g. 'sar-images'
  name       TEXT NOT NULL UNIQUE,
  local_only INTEGER NOT NULL DEFAULT 1,        -- 1 => never cloud LLM (ADR-003)
  mcp_exclude INTEGER NOT NULL DEFAULT 0,       -- 1 => never returned via MCP (ADR-022; migration 0003)
  allowed    INTEGER NOT NULL DEFAULT 0,        -- 0 => inert until enabled (ADR-002)
  created_at TEXT NOT NULL
);

CREATE TABLE project_roots (
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  root_path  TEXT NOT NULL,                     -- absolute path
  PRIMARY KEY (project_id, root_path)
);
```

Why: `sources` is the permission record (addition #2: enabled, allowed paths,
last sync, seen schema version). `projects` + `project_roots` separate identity from
paths so `config/projects.toml` can group many directories into one project
(addition #4). Event → project linking = **longest root_path prefix of the event's
working directory**; one root covers its whole subtree, deeper roots shadow shallower
ones. Seeding: 11 of the 12 Phase-0-observed dirs start `allowed=1` (ADR-002); the
Home catch-all starts `allowed=0` (ADR-016), as do projects discovered later.

`config/projects.toml` (loaded at every sync; overrides seeds):

```toml
[projects.sar-images]
name = "SAR Images"
allowed = true
roots = ["/Users/himanshu/Desktop/SAR Images"]   # covers subdirs via prefix matching
```

## Sessions

```sql
CREATE TABLE sessions (
  id                TEXT PRIMARY KEY,           -- '<source>:<source_session_id>'
  source            TEXT NOT NULL,
  source_session_id TEXT NOT NULL,
  project_id        TEXT REFERENCES projects(id),
  started_at        INTEGER NOT NULL,           -- epoch ms
  ended_at          INTEGER,
  cwd               TEXT,                       -- best-known working directory
  model             TEXT,                       -- 'providerID/modelID'
  agent             TEXT,                       -- OpenCode: 'build' | 'plan'
  title             TEXT,
  UNIQUE (source, source_session_id)
);
```

Why: mirrors native sessions so citations can point at something the user can open
in the source tool. OpenCode's own `project_id` is **deliberately not used** for
linking — 37/47 observed sessions map to its fallback project `/`; we link via
`session.directory` / per-message `path.cwd` instead (ADR-009).

## Events

```sql
CREATE TABLE events (
  id             INTEGER PRIMARY KEY,
  source         TEXT NOT NULL,
  source_id      TEXT NOT NULL,                -- native id (OpenCode part id; prompt line hash)
  ts             INTEGER NOT NULL,             -- epoch ms
  project_id     TEXT REFERENCES projects(id),
  session_id     TEXT REFERENCES sessions(id),
  role           TEXT,                         -- 'user' | 'assistant' | NULL
  kind           TEXT NOT NULL,                -- text | reasoning | tool:<name> | tool_result | patch | file | compaction | prompt
  call_id        TEXT,                         -- links a tool result to its call
  message_id     TEXT,                         -- source message id (migration 0002, ADR-018); citations
  synthetic      INTEGER NOT NULL DEFAULT 0,   -- system-injected content
  text           TEXT NOT NULL,                -- REDACTED; head+tail if truncated
  token_estimate INTEGER NOT NULL,
  truncated      INTEGER NOT NULL DEFAULT 0,
  blob_ref       TEXT,                         -- 'blobs/<aa>/<sha256>' relative to data dir
  raw_json       TEXT,                         -- source JSON; NULL when blob_ref set (blob IS the raw record)
  content_hash   TEXT NOT NULL,                -- sha256 of canonical source content
  UNIQUE (source, source_id)
);
CREATE INDEX events_session_idx ON events(session_id, ts);
CREATE INDEX events_project_idx ON events(project_id, ts);

CREATE VIRTUAL TABLE events_fts USING fts5(
  text, content='events', content_rowid='id', tokenize='porter unicode61'
);
-- maintained with ('delete' | 'insert') triggers, or rebuilt by `ctx reindex`
```

Why: one immutable row per source artifact. `kind` keeps the OpenCode part taxonomy
(tool parts become `tool:<name>`, e.g. `tool:bash`) plus `prompt` for
prompt-history lines. `call_id` preserves tool-call ↔ result linkage. `content_hash`
makes changed-source detection cheap. `raw_json` is omitted for spilled parts — the
blob holds the full raw record, so nothing is lost by truncating `text`.

**Blob policy** (ADR-004): content > 64 K characters → write
`data/blobs/<aa>/<sha256>` (fsync) **before** inserting the referencing event;
`text` keeps head+tail (~4 K characters each). Blobs store **redacted** content —
redaction precedes spilling (ADR-012) — and are content-addressed: identical
content is stored once.

## Derived: sync_state (incremental cursors; migration 0004, ADR-024)

```sql
CREATE TABLE sync_state (
  source     TEXT NOT NULL,
  key        TEXT NOT NULL,          -- e.g. 'file:/abs/path.jsonl', 'db:/abs/state.db'
  value      TEXT NOT NULL,          -- JSON; today [[size, mtime_ns], ...] fingerprints
  updated_at TEXT NOT NULL,
  PRIMARY KEY (source, key)
);
```

Not events and not user state: derived and rebuildable. Deleting rows (or the whole
table's contents) only causes a full re-read on the next sync; `UNIQUE(source,
source_id)` guarantees zero duplicates. Written in the same transaction as the events
they describe.

## Derived: episodes & facts (rebuildable)

```sql
CREATE TABLE episodes (
  id            INTEGER PRIMARY KEY,
  project_id    TEXT REFERENCES projects(id),  -- NULL = events linked to no project
  start_ts      INTEGER NOT NULL,              -- epoch ms
  end_ts        INTEGER NOT NULL,
  summary       TEXT,                          -- LLM-generated later; NULL until then
  summary_model TEXT,
  created_at    TEXT NOT NULL
);

CREATE TABLE episode_events (
  episode_id INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
  event_id   INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  PRIMARY KEY (episode_id, event_id)
);

CREATE TABLE facts (
  id                 INTEGER PRIMARY KEY,
  episode_id         INTEGER REFERENCES episodes(id) ON DELETE CASCADE,
  type               TEXT NOT NULL CHECK (type IN ('decision','issue','todo','rejected','setup')),
  text               TEXT NOT NULL,
  status             TEXT NOT NULL DEFAULT 'suggested'
                       CHECK (status IN ('suggested','confirmed','abandoned')),
  evidence_event_ids TEXT NOT NULL,            -- JSON array of events.id
  created_at         TEXT NOT NULL
);
```

Why: an **episode** is a deterministic grouping — same project, events separated by
< 45 min of idle time (configurable). Facts are the LLM layer; they must cite
evidence and never assert a decision the user didn't make (`status` lifecycle).
Both tables can be dropped and rebuilt from `events` alone.

Episode rules (implemented in `core/episodes.py`, ADR-026): an event's project is
`COALESCE(sessions.project_id, events.project_id)`; events of one project from **all
sources** are sorted by `(ts, id)` and split wherever consecutive events are
`>= gap` apart (default 45 min, so exactly 45:00 splits; `CTX_EPISODE_GAP_MINUTES` or
`ctx reindex --gap`). Events with no project (web-chat imports) form one NULL-project
bucket, shown only with `--all`. `episodes.id` is assigned 1..n ordered by
`(start_ts, project_id, first event id)` on each rebuild, so unchanged events give
identical ids; ids are not stable when new events arrive. `episode_events` lists every
member event (all kinds). Migration `0005` made `project_id` nullable and added indexes.
The timeline's per-day split, counts, titles, top files and commands are computed at
query time from events and are not stored.

## Summaries (derived, regenerable)

```sql
CREATE TABLE summaries (
  id INTEGER PRIMARY KEY,
  scope TEXT NOT NULL CHECK (scope IN ('episode','day')),
  scope_key TEXT NOT NULL,         -- episode: its start_ts (ms); day: local YYYY-MM-DD
  project_id TEXT NOT NULL DEFAULT '',   -- '' = events linked to no project
  start_ts INTEGER NOT NULL, end_ts INTEGER NOT NULL,
  text TEXT NOT NULL,              -- redacted; inline [E<event id>] citations
  provider TEXT NOT NULL, model TEXT NOT NULL, prompt_version TEXT NOT NULL,
  input_hash TEXT NOT NULL,        -- sha256(prompt version, provider, model, event ids)
  created_at TEXT NOT NULL,
  UNIQUE (scope, scope_key, project_id)
);
CREATE TABLE summary_events (
  summary_id INTEGER NOT NULL REFERENCES summaries(id) ON DELETE CASCADE,
  event_id   INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  cited      INTEGER NOT NULL DEFAULT 0,   -- 1 = the model also cited it inline (valid id)
  PRIMARY KEY (summary_id, event_id)
);
```

Migration `0007`, written by `ctx summarize` (ADR-029). Links are deterministic: every event
that reached the prompt is linked; the model only sets `cited`. A summary whose citations
include an id outside the context, or none, is not stored. No FK to `episodes` because
`ctx reindex` renumbers episodes (an episode summary is keyed by its `start_ts`). Not
touched by `ctx reindex`; dropping both tables loses no source data (re-run `ctx summarize`).
A cloud-provider summary exists only for `local_only=0` projects. The special project id
`unlinked` (row created by `ctx project set unlinked`) carries the cloud opt-in for events
with no project.

## Embeddings (optional, derived)

```sql
CREATE TABLE embeddings (
  event_id   INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  chunk_idx  INTEGER NOT NULL,                 -- deterministic chunk number within the event
  model      TEXT NOT NULL,                    -- embedder spec, e.g. 'model2vec:minishlab/potion-base-8M'
  dim        INTEGER NOT NULL,
  vector     BLOB NOT NULL,                    -- float32 x dim, L2-normalized (cosine = dot)
  text_hash  TEXT NOT NULL,                    -- sha256 of the redacted chunk text
  PRIMARY KEY (event_id, chunk_idx, model)
);
CREATE INDEX embeddings_hash_idx ON embeddings(model, text_hash);
```

Migration `0006`. Derived and rebuildable from `events` alone (`ctx embed --rebuild`);
safe to drop. Chunking (`core/embed.py:chunk_text`) is a pure function of the redacted
text: paragraphs, then lines, then fixed-width slices, packed up to 1000 characters, at
most 16 chunks per event (head only; FTS still covers the rest). `ctx embed` is
incremental: a chunk whose `(event, idx, text_hash)` row exists is skipped, identical
text reuses its vector, only new text reaches the embedder. Vectors of different
`model` values live side by side; search uses the configured model only. Vector search
is brute-force cosine in numpy over the rows that pass the same filters as FTS.

## Data directory layout

The data dir is `$CTX_HOME`, else `$CTX_DATA_DIR`, else a per-OS user dir (macOS
`~/Library/Application Support/ctx`, Linux `$XDG_DATA_HOME/ctx` or
`~/.local/share/ctx`, Windows `%LOCALAPPDATA%\ctx`). Project config is
`$CTX_CONFIG`, else `<data dir>/projects.toml`, else the repo's `config/projects.toml`.

```
<data dir>/
  ctx.db                      SQLite database (WAL)
  blobs/<aa>/<sha256>         spilled raw content, content-addressed
  tmp/                        sync snapshots (e.g. opencode snapshot copy)
```

The repo-local `data/` is gitignored. `AGENTS.md` forbids committing it.
