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
      *(Implemented and fixture-tested; the run against the real 47-session DB is
      still to be done by the user: `ctx sync --dry-run`, then `ctx sync`.)*
- [x] A second `ctx sync` creates **zero duplicates** (contract test).
- [x] `ctx search "phrase"` returns excerpts with session/message references and
      respects `--project`, `--since`, `--source`, `--all`.
- [x] Redaction runs **before** indexing; unit test with fake secrets (API keys,
      bearer tokens, `.env` values, private keys) proves none survive into
      `events`/FTS.
- [x] Tool outputs > 64 KB are truncated (head+tail) with a blob reference written
      **before** the event insert; every event has `token_estimate`.
- [ ] Prompt-history connector imports `prompt-history.jsonl` as `kind='prompt'` events.
      *(Deferred: not yet implemented.)*
- [ ] Source permission records work: `ctx sources list | add | show` (enabled,
      allowed paths, last sync, schema version seen); denylisted tables/paths are
      never read (contract test). *(`list` and `show` done; `add` deferred.)*
- [x] OpenCode connector fails loudly on unrecognized schema/version, before any writes.
- [x] Shared connector contract suite exists and the OpenCode connectors pass it.
- [x] Fixture-based test uses a small anonymized sample of the real session format.
- [x] `build_context(query, budget_tokens, project)` exists with v1 behavior
      (top-N FTS5 trimmed to budget) and a unit test; returns evidence IDs and
      what was left out.

## M1.5 — Coding-agent connectors: Claude Code, Codex

Later in this family: Hermes, Gemini CLI.

- [x] `claude_code` connector reads `~/.claude/projects/*/*.jsonl`: messages, text,
      tool_use/tool_result (large outputs spill to blobs), cwd project linking,
      sidechain/unknown line types counted and skipped, malformed lines tolerated.
- [x] `codex` connector reads `~/.codex/sessions/**/rollout-*.jsonl` (`.zst` only if
      `zstandard` is installed, else skipped with a warning).
- [x] Both declare `schema_version_pin`, `allowed_paths`, `denylist_paths`; fail loud on
      unknown shape; pass the shared contract suite; dry-run report via
      `python -m ctx.connectors.<name>` and `ctx sync --dry-run`.
- [x] Fake token in a message reaches `events`/FTS redacted; `ctx sync --source
      claude_code|codex` is idempotent; an appended transcript adds only new events.
- [ ] Codex verified against real rollout files (none on the dev machine yet).
- [ ] Size/mtime skip-cache for incremental sync (needs an engine state seam).

## M1.6 — Hermes, Devin CLI, Antigravity CLI connectors

- [x] `hermes` connector: `$HERMES_HOME/state.db` + `profiles/*/state.db` (profile-
      namespaced ids), WAL-safe snapshot, FTS/gateway/room tables denylisted.
- [x] `devin` connector: `cli/sessions.db` (`CHISEL_SESSION_DB` honored), compaction
      node copies deduped, `credentials.toml`/`mcp/oauth` denylisted.
- [x] `antigravity_cli` connector: `brain/*/transcript_full.jsonl` + summaries DB +
      `last_conversations.json` project linking (`AGY_CONVERSATIONS_DIR` honored);
      protobuf-only conversations counted as a warning.
- [x] All three pass the shared contract suite, fail loud on unknown shape, are
      idempotent, redact before index; `ctx sync --source <name> [--dry-run]` tested.
- [ ] Hermes `profiles/*` verified on a real install (none on the dev machine).
- [ ] Antigravity protobuf `steps` / `conversations/*.pb` decoding (5 of 49 real
      conversations have no transcript); Antigravity IDE connector (separate source).
- [x] Plain `ctx sync` skips sources whose storage is absent (`not installed`, exit 0);
      `--source X` on an absent source is an error (ADR-024).
- [x] Incremental sync (`sync_state`), `ctx sync --full`, `ctx status`, `ctx doctor` (ADR-024).

## M1.7 — Web chat history via official exports

- [x] `chatgpt`, `claude_ai`, `gemini` connectors over the zips users download
      (ChatGPT "Export data", Claude.ai export, Google Takeout > My Activity > Gemini
      Apps); zips read in place, denylisted members never opened, contract suite green.
- [x] `ctx import <chatgpt|claude-ai|gemini> PATH` and the watch dir
      `<CTX_HOME>/imports/<source>/` scanned by plain `ctx sync` (ADR-025).
- [x] Superset re-import adds only new messages; web chats stay unlinked to projects.
- [ ] Verify against real exports (only synthetic, real-shaped fixtures so far);
      localized Takeout folder names / HTML labels are unsupported.
- [ ] **Later, not scheduled:** a browser extension for live capture of web chats. It
      would need its own threat model and ADR; exports are the only web path today.

## M2 — Timeline & episodes (deterministic, no LLM)

- [x] Episodes computed deterministically (same project + < 45 min idle gap,
      configurable via `CTX_EPISODE_GAP_MINUTES` / `ctx reindex --gap`; sources merged;
      rebuilt by `ctx sync` and `ctx reindex`, same ids for same events; ADR-026).
- [x] `ctx timeline [--project --source --since --until --day --tz --all --json]`
      renders a browsable per-day, per-project history across all sources with message
      and tool-call counts, session titles, and top files / commands (replacing the
      originally planned "top terms", which were not derivable deterministically
      without picking a tokenizer/stoplist; revisit with M5).
- [x] `ctx project list | show <name>` shows roots, allow/local_only/mcp_exclude state,
      stats.
- [x] MCP `timeline` tool (read-only, redacted, `mcp_exclude` respected, capped).

## M3 — Handoff

- [ ] `ctx handoff <project>` emits a concise Markdown context package (state,
      open TODOs, recent decisions) built **only** via `build_context`.

## M4 — Eval set

- [x] Harness: `ctx eval run FILE [--mode fts|vector|hybrid|all] [--k 10] [--json]` reports
      recall@k, MRR and nDCG@k per retrieval mode, side by side (ADR-028). It scores
      `retrieve`, the single ranking path under `build_context`.
- [x] Synthetic multi-source corpus + 20 questions (paraphrase, exact error strings, file
      names, "why did I choose X") in `tests/fixtures/`; CI compares modes with a
      deterministic fake embedder.
- [ ] 15–20 **real** questions from my history with known answers, kept in
      `<CTX_HOME>/eval/` (never committed; see `docs/CONTEXT_ENGINEERING.md`). User task.
- [ ] Extend the harness to score whole `build_context` packages (token budget effects),
      not just retrieval ranking.

## M4.5 — Hybrid search (embeddings)

- [x] Derived `embeddings` table (migration 0006), chunked redacted text only, rebuildable;
      `ctx embed [--rebuild] [--model]`, incremental by text hash; optional
      `CTX_EMBED_ON_SYNC=1`.
- [x] Local embedder via optional extra `embed` (model2vec, ADR-027); base install stays
      FTS-only with a clear message.
- [x] `ctx search --mode auto|fts|vector|hybrid`; hybrid = RRF(FTS, vector, k=60); filters
      identical in all modes; `build_context` and MCP use the same `retrieve`.
- [x] Cloud embedders are refused for `local_only=1` projects (interface + test with a
      fake cloud embedder); no cloud embedder ships until the providers milestone.
- [ ] sqlite-vec / ANN index when brute force gets slow (not needed at current scale).
- [ ] Tune chunk size, RRF k, and model choice on the real personal eval set.

## M5 — Synthesis with evidence links

- [ ] LLM provider abstraction: `LocalProvider` (Ollama) + `OpenAICompatibleProvider`
      (NVIDIA; base URL/key from `.env`, never committed).
- [ ] Episode summaries + fact extraction, every fact carrying `evidence_event_ids`
      and `status ∈ {suggested, confirmed, abandoned}`.
- [ ] Cloud calls only for projects with `local_only=0` (all start `local_only=1`).
- [ ] Derived tables provably rebuildable from `events` alone.

## M6 — MCP server (before Antigravity)

- [x] `ctx serve-mcp` (stdio, official `mcp` SDK) exposes `search`, `get_context`,
      `get_session`, `list_projects`, `recent_sessions` and an optional `sync` tool.
      Context only through `build_context`; all output re-redacted and size-capped.
      Pulled forward ahead of M2 (ADR-022). `timeline` landed with M2 (ADR-026).
- [x] `mcp_exclude` per-project opt-out; `ctx mcp config --client ...` prints (never
      writes) client config. Only the Claude Code snippet is verified.

## M7 — Antigravity connector (CLI done in M1.6)

- [x] Antigravity CLI transcript connector (see M1.6).
- [ ] Antigravity IDE connector, `brain/` markdown artifacts as file events, and
      protobuf `steps` decoding.

## M8 — VS Code + shell history connectors

- [ ] As `docs/CONNECTORS.md` §4–5; pass contract suite.
- [ ] Planned (not started): web chat exports (ChatGPT/Claude.ai export files) as a file-import connector.

## M9 — Context engineering behind the seam

- [ ] Techniques from `docs/CONTEXT_ENGINEERING.md` adopted only after beating v1
      on the M4 eval set (hybrid retrieval is the first, measured on the synthetic set
      in ADR-027; confirm on the personal set).

## M10 — GUI (only if the CLI proves useful)

- [ ] Explicit go-ahead required before any GUI work.
