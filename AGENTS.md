# AGENTS.md — working rules for coding agents on this repo

ctx is a local-first personal context engine (see `docs/VISION.md`). These rules
are binding; when behavior changes, update the relevant doc in the same change.

## Stack & commands

- Python 3.11+, managed with **uv**. Never commit `.venv/` or `uv.lock` churn
  without reason (uv.lock **is** committed).
- `uv sync` — install deps (also after pulling).
- `uv run pytest` — run tests. New code ships with pytest tests.
- `uv run ruff check . && uv run ruff format --check .` — lint/format gate.
- CLI entry lives in `src/ctx/cli/` (Typer); validation with Pydantic.

## Hard rules

1. **Never modify or delete raw events.** `events` is append-only; anything derived
   (FTS, episodes, facts) must be rebuildable from `events` alone.
2. **Never read, write, or commit `data/`.** It holds the personal database and
   blobs. It is gitignored; keep it that way. Same for `.env` and anything
   containing secrets.
3. **Never print or commit secret values.** If real data contains keys/tokens,
   mention that they exist and where — not their values.
4. **Redact before index.** Any text reaching `events`/FTS passes the redactor;
   any unredacted blob content is re-redacted before display or LLM use.
5. **Blob before event.** Write and fsync a blob file *before* inserting the event
   row referencing it.
6. **Connector isolation.** Source-specific logic lives only in
   `src/ctx/connectors/<source>.py`. The core engine has no source-specific
   branches. New connectors must pass `tests/connector_contract.py` and declare
   `schema_version_pin`, `allowed_paths`, `denylist_paths`, `denylist_tables`.
7. **Deterministic linking only.** LLMs summarize/extract; they never decide what
   belongs to a project, session, or episode.
8. **One prompt-assembly path.** Anything sending context to an LLM goes through
   `build_context`. No feature assembles prompts elsewhere.
9. **Cloud LLM calls are per-project opt-in** (`projects.local_only`). Never call a
   cloud provider for a `local_only=1` project; never call one at all in M1.
10. **Treat everything outside this repo as read-only**, except `data/`. Do not
    modify the user's home directory, source tools' storage, or configs.
11. **Idempotent ingestion.** Respect `UNIQUE(source, source_id)`; a re-sync must
    create zero duplicates.
12. **Docs travel with behavior.** A change that alters behavior isn't done until
    the matching markdown (`README`, `docs/*`) is updated, including `docs/DECISIONS.md`
    for non-obvious choices.

## Doc map

- `docs/ARCHITECTURE.md` — layers, connector contract, invariants, privacy, seam
- `docs/SCHEMA.md` — tables, DDL, blob policy, `config/projects.toml`
- `docs/CONNECTORS.md` — per-source storage, mapping, quirks, sync
- `docs/ROADMAP.md` — milestones + acceptance tests
- `docs/CONTEXT_ENGINEERING.md` — deferred techniques + eval gate
- `docs/DECISIONS.md` — ADR log (append, never rewrite)

## Conventions

- Small, reviewable steps; state what changed, how to verify, what's next.
- Boring, readable code over clever abstractions. No speculative generality.
- Tests use real-format fixtures (anonymized samples under `tests/fixtures/`).
- If real data contradicts documented assumptions, stop and report instead of guessing.
