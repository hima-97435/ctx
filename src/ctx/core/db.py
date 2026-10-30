"""SQLite connection management and schema migrations.

The schema matches docs/SCHEMA.md. Migrations run in order inside a transaction;
each is recorded in `schema_migrations` and never re-applied.
"""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

_MIGRATION_0001_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE sources (
      id                  INTEGER PRIMARY KEY,
      name                TEXT NOT NULL UNIQUE,
      enabled             INTEGER NOT NULL DEFAULT 1,
      allowed_paths       TEXT NOT NULL DEFAULT '[]',
      schema_version_seen TEXT,
      last_sync           TEXT,
      created_at          TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE projects (
      id         TEXT PRIMARY KEY,
      name       TEXT NOT NULL UNIQUE,
      local_only INTEGER NOT NULL DEFAULT 1,
      allowed    INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE project_roots (
      project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
      root_path  TEXT NOT NULL,
      PRIMARY KEY (project_id, root_path)
    )
    """,
    """
    CREATE TABLE sessions (
      id                TEXT PRIMARY KEY,
      source            TEXT NOT NULL,
      source_session_id TEXT NOT NULL,
      project_id        TEXT REFERENCES projects(id),
      started_at        INTEGER NOT NULL,
      ended_at          INTEGER,
      cwd               TEXT,
      model             TEXT,
      agent             TEXT,
      title             TEXT,
      UNIQUE (source, source_session_id)
    )
    """,
    """
    CREATE TABLE events (
      id             INTEGER PRIMARY KEY,
      source         TEXT NOT NULL,
      source_id      TEXT NOT NULL,
      ts             INTEGER NOT NULL,
      project_id     TEXT REFERENCES projects(id),
      session_id     TEXT REFERENCES sessions(id),
      role           TEXT,
      kind           TEXT NOT NULL,
      call_id        TEXT,
      synthetic      INTEGER NOT NULL DEFAULT 0,
      text           TEXT NOT NULL,
      token_estimate INTEGER NOT NULL,
      truncated      INTEGER NOT NULL DEFAULT 0,
      blob_ref       TEXT,
      raw_json       TEXT,
      content_hash   TEXT NOT NULL,
      UNIQUE (source, source_id)
    )
    """,
    "CREATE INDEX events_session_idx ON events(session_id, ts)",
    "CREATE INDEX events_project_idx ON events(project_id, ts)",
    """
    CREATE VIRTUAL TABLE events_fts USING fts5(
      text, content='events', content_rowid='id', tokenize='porter unicode61'
    )
    """,
    """
    CREATE TRIGGER events_fts_ai AFTER INSERT ON events BEGIN
      INSERT INTO events_fts(rowid, text) VALUES (new.id, new.text);
    END
    """,
    """
    CREATE TRIGGER events_fts_ad AFTER DELETE ON events BEGIN
      INSERT INTO events_fts(events_fts, rowid, text) VALUES ('delete', old.id, old.text);
    END
    """,
    """
    CREATE TRIGGER events_fts_au AFTER UPDATE ON events BEGIN
      INSERT INTO events_fts(events_fts, rowid, text) VALUES ('delete', old.id, old.text);
      INSERT INTO events_fts(rowid, text) VALUES (new.id, new.text);
    END
    """,
    """
    CREATE TABLE episodes (
      id            INTEGER PRIMARY KEY,
      project_id    TEXT NOT NULL REFERENCES projects(id),
      start_ts      INTEGER NOT NULL,
      end_ts        INTEGER NOT NULL,
      summary       TEXT,
      summary_model TEXT,
      created_at    TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE episode_events (
      episode_id INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
      event_id   INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
      PRIMARY KEY (episode_id, event_id)
    )
    """,
    """
    CREATE TABLE facts (
      id                 INTEGER PRIMARY KEY,
      episode_id         INTEGER REFERENCES episodes(id) ON DELETE CASCADE,
      type               TEXT NOT NULL
                           CHECK (type IN ('decision','issue','todo','rejected','setup')),
      text               TEXT NOT NULL,
      status             TEXT NOT NULL DEFAULT 'suggested'
                           CHECK (status IN ('suggested','confirmed','abandoned')),
      evidence_event_ids TEXT NOT NULL,
      created_at         TEXT NOT NULL
    )
    """,
)

# Citations need the source message an event came from (ADR-018).
_MIGRATION_0002_STATEMENTS: tuple[str, ...] = ("ALTER TABLE events ADD COLUMN message_id TEXT",)

MIGRATIONS: list[tuple[str, tuple[str, ...]]] = [
    ("0001_initial", _MIGRATION_0001_STATEMENTS),
    ("0002_event_message_id", _MIGRATION_0002_STATEMENTS),
]


def connect(db_path: Path) -> sqlite3.Connection:
    """Open (creating if needed) the ctx database with pragmas applied and schema migrated."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None  # explicit transaction control
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    applied = {row["name"] for row in conn.execute("SELECT name FROM schema_migrations")}
    for name, statements in MIGRATIONS:
        if name in applied:
            continue
        now = datetime.now(UTC).isoformat(timespec="seconds")
        try:
            conn.execute("BEGIN")
            for statement in statements:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations(name, applied_at) VALUES (?, ?)",
                (name, now),
            )
            conn.execute("COMMIT")
        except Exception:
            _rollback_quietly(conn)
            raise


def _rollback_quietly(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass
