import sqlite3

from ctx.core.db import connect
from ctx.core.paths import data_dir, db_path, default_data_dir, repo_root

EXPECTED_TABLES = {
    "sources",
    "projects",
    "project_roots",
    "sessions",
    "events",
    "events_fts",
    "episodes",
    "episode_events",
    "facts",
    "schema_migrations",
    "embeddings",
    "summaries",
    "summary_events",
}


def test_migrate_creates_full_schema(tmp_path):
    conn = connect(tmp_path / "ctx.db")
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")
    names = {r["name"] for r in rows}
    assert EXPECTED_TABLES <= names
    applied = {r["name"] for r in conn.execute("SELECT name FROM schema_migrations")}
    assert applied == {
        "0001_initial",
        "0002_event_message_id",
        "0003_project_mcp_exclude",
        "0004_sync_state",
        "0005_episodes_nullable_project",
        "0006_embeddings",
        "0007_summaries",
    }


def test_migrate_is_idempotent(tmp_path):
    path = tmp_path / "ctx.db"
    conn1 = connect(path)
    conn2 = connect(path)  # reopen same file: must not re-apply or fail
    count = conn2.execute("SELECT count(*) FROM schema_migrations").fetchone()[0]
    assert count == 7
    assert conn1 is not conn2


def test_events_fts_stays_in_sync(tmp_path):
    conn = connect(tmp_path / "ctx.db")
    conn.execute(
        "INSERT INTO events (source, source_id, ts, kind, text, token_estimate, content_hash)"
        " VALUES ('t', '1', 0, 'text', 'hello crimson world', 3, 'h1')"
    )
    hits = conn.execute("SELECT rowid FROM events_fts WHERE events_fts MATCH 'crimson'").fetchall()
    assert len(hits) == 1

    conn.execute(
        "UPDATE events SET text = 'hello amber world' WHERE source = 't' AND source_id = '1'"
    )
    assert (
        conn.execute("SELECT rowid FROM events_fts WHERE events_fts MATCH 'crimson'").fetchone()
        is None
    )
    assert (
        conn.execute("SELECT rowid FROM events_fts WHERE events_fts MATCH 'amber'").fetchone()
        is not None
    )

    conn.execute("DELETE FROM events WHERE source = 't'")
    assert (
        conn.execute("SELECT rowid FROM events_fts WHERE events_fts MATCH 'amber'").fetchone()
        is None
    )


def test_pragmas_and_fk_enforcement(tmp_path):
    conn = connect(tmp_path / "ctx.db")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    try:
        conn.execute(
            "INSERT INTO events (source, source_id, ts, kind, text, token_estimate,"
            " content_hash, session_id) VALUES ('t', '2', 0, 'text', 'x', 1, 'h2', 'nope')"
        )
        raised = False
    except sqlite3.IntegrityError:
        raised = True
    assert raised, "expected FK violation for unknown session_id"


def test_event_unique_source_source_id(tmp_path):
    conn = connect(tmp_path / "ctx.db")
    sql = (
        "INSERT INTO events (source, source_id, ts, kind, text, token_estimate, content_hash)"
        " VALUES ('t', 'dup', 0, 'text', 'x', 1, 'h')"
    )
    conn.execute(sql)
    try:
        conn.execute(sql)
        raised = False
    except sqlite3.IntegrityError:
        raised = True
    assert raised, "expected UNIQUE(source, source_id) violation"


def test_paths_defaults_and_env_override(monkeypatch, tmp_path):
    monkeypatch.delenv("CTX_HOME", raising=False)
    monkeypatch.delenv("CTX_DATA_DIR", raising=False)
    assert data_dir() == default_data_dir()
    assert repo_root() not in data_dir().parents  # repo-independent default
    monkeypatch.setenv("CTX_DATA_DIR", str(tmp_path))
    assert db_path() == tmp_path.resolve() / "ctx.db"
    other = tmp_path / "home"
    monkeypatch.setenv("CTX_HOME", str(other))  # CTX_HOME wins over the legacy var
    assert db_path() == other.resolve() / "ctx.db"
