import sqlite3

import pytest
from typer.testing import CliRunner

from ctx.cli.main import app
from ctx.connectors import sqlite_common
from ctx.connectors.base import ConnectorError, DenylistViolation
from ctx.connectors.hermes import HermesConnector, default_home
from ctx.connectors.jsonl_common import dry_run_report
from ctx.connectors.registry import available_connectors
from ctx.core.blobs import BlobStore
from ctx.core.db import connect
from ctx.core.model import RawEvent, RawSession
from ctx.core.projects import load_projects_config
from ctx.core.search import search
from ctx.core.sync import sync
from tests.fixtures.hermes_db import FAKE_TOKEN, build_hermes_db, build_hermes_home
from tests.helpers import PROJECTS_TOML


@pytest.fixture()
def conn_root(tmp_path):
    connector = HermesConnector(tmp_path / "hermes")
    return connector, build_hermes_home(tmp_path / "hermes")


def fetch(connector, path, tmp_path):
    store = BlobStore(tmp_path / "blobs")
    return list(connector.fetch(path, blob_store=store, tmp_dir=tmp_path / "t")), store


def split(records):
    sessions = {r.source_session_id: r for r in records if isinstance(r, RawSession)}
    events = {r.source_id: r for r in records if isinstance(r, RawEvent)}
    return sessions, events


def test_sessions_and_mapping(conn_root, tmp_path):
    connector, path = conn_root
    sessions, events = split(fetch(connector, path, tmp_path)[0])
    assert set(sessions) == {"s1", "s2", "work/s1", "work/s2"}
    s = sessions["s1"]
    assert s.cwd == "/tmp/proj" and s.model == "hermes-test-1" and s.title == "Zebra migration"
    assert s.started_at == 1_790_848_800_500 and s.agent == "hermes"
    assert s.ended_at == 1_790_848_810_500  # no ended_at stored -> last message
    assert sessions["s2"].cwd is None  # no cwd stored -> unlinked
    assert sessions["work/s1"].started_at == 1_790_848_800_500 + 5_000_000

    assert events["s1:m1:text"].role == "user" and events["s1:m1:text"].message_ref == "s1:m1"
    assert events["s1:m1:text"].ts == 1_790_848_801_500
    assert events["s1:2:reasoning"].kind == "reasoning"
    assert events["s1:2:reasoning"].role == "assistant"
    assert events["s1:2:text"].text == "Let me look."
    call = events["s1:2:call0"]
    assert call.kind == "tool:terminal" and call.call_id == "call_1"
    assert '"command": "ls"' in call.text and call.role == "assistant"
    res = events["s1:3:result"]
    assert res.kind == "tool_result" and res.call_id == "call_1" and res.role is None
    assert "file1" in res.text and res.synthetic is False
    env = events["s1:5:result"]
    assert "PLAIN=[REDACTED" in env.text  # .env read -> whole output file-redacted
    assert events["s1:8:text"].role is None and events["s1:8:text"].synthetic is True  # system
    assert events["s1:9:text"].synthetic is True  # compressed summary
    assert events["s1:10:text"].synthetic is False and events["s1:10:text"].text == "done"
    assert not any(k.startswith("s1:4:text") for k in events)  # empty content skipped


def test_profile_ids_do_not_collide(conn_root, tmp_path):
    connector, path = conn_root
    records, _ = fetch(connector, path, tmp_path)
    _, events = split(records)
    ids = [r.source_id for r in records if isinstance(r, RawEvent)]
    assert len(ids) == len(set(ids))
    assert "work/s1:m1:text" in events and "s1:m1:text" in events
    assert "work/s1:2:text" in events and "s1:2:text" in events
    first_event = next(i for i, r in enumerate(records) if isinstance(r, RawEvent))
    assert all(isinstance(r, RawSession) for r in records[:first_event])
    assert first_event == 4  # sessions of both profiles precede every event


def test_large_output_spills_to_redacted_blob(conn_root, tmp_path):
    connector, path = conn_root
    records, store = fetch(connector, path, tmp_path)
    big = next(r for r in records if isinstance(r, RawEvent) and r.source_id == "s1:7:result")
    assert big.truncated and big.blob_ref and store.exists(big.blob_ref)
    blob = store.read(big.blob_ref)
    assert "deepsecretvalue123" not in blob and "[REDACTED" in blob


def test_single_db_file_and_profile_dir(conn_root, tmp_path):
    connector, root = conn_root
    sessions, _ = split(fetch(connector, root / "state.db", tmp_path / "a")[0])
    assert set(sessions) == {"s1", "s2"}
    sessions, _ = split(
        fetch(connector, root / "profiles" / "work" / "state.db", tmp_path / "b")[0]
    )
    assert set(sessions) == {"work/s1", "work/s2"}


def test_malformed_and_orphan_rows_are_tolerated(tmp_path):
    connector = HermesConnector(tmp_path / "hermes")
    db = build_hermes_db(tmp_path / "hermes" / "state.db")
    c = sqlite3.connect(db)
    c.execute(
        "INSERT INTO messages(session_id, role, content, timestamp) VALUES ('s1','user','x',NULL)"
    )
    c.execute(
        "INSERT INTO messages(session_id, role, content, tool_calls, timestamp)"
        " VALUES ('s1','assistant','y','not json', 1790848900.0)"
    )
    c.execute(
        "INSERT INTO messages(session_id, role, content, timestamp)"
        " VALUES ('ghost','user','orphan text', 1790848901.0)"
    )
    c.commit()
    c.close()
    records, _ = fetch(connector, db, tmp_path)
    sessions, events = split(records)
    assert "ghost" in sessions and any(e.session_ref == "ghost" for e in events.values())
    assert connector.last_stats.skipped["malformed_record"] == 1


def test_incremental_append_adds_only_new_events(conn_root, tmp_path):
    connector, root = conn_root
    db = connect(tmp_path / "ctx.db")
    kw = {"source_paths": {"hermes": root}, "data_dir": tmp_path / "data"}
    first = sync(db, {"hermes": connector}, **kw)[0]
    assert first.status == "ok"
    c = sqlite3.connect(root / "state.db")
    c.execute(
        "INSERT INTO messages(session_id, role, content, timestamp) VALUES ('s1','user',"
        "'later quokka', 1790849999.0)"
    )
    c.commit()
    c.close()
    second = sync(db, {"hermes": connector}, **kw)[0]
    assert second.events_new == 1 and second.events_duplicate == first.events_new
    assert search(db, "quokka", include_all=True)


def test_project_linking_via_cwd(conn_root, tmp_path):
    connector, root = conn_root
    db = connect(tmp_path / "ctx.db")
    cfg = tmp_path / "projects.toml"
    cfg.write_text(PROJECTS_TOML)
    sync(
        db,
        {"hermes": connector},
        source_paths={"hermes": root},
        project_configs=load_projects_config(cfg),
        data_dir=tmp_path / "data",
    )
    rows = {r[0]: r[1] for r in db.execute("SELECT source_session_id, project_id FROM sessions")}
    assert rows["s1"] == "proj" and rows["work/s1"] == "proj"
    assert rows["s2"] != "proj"  # no cwd stored: not linked to a project by path


def test_fake_token_redacted_in_fts(conn_root, tmp_path):
    connector, root = conn_root
    db = connect(tmp_path / "ctx.db")
    sync(db, {"hermes": connector}, source_paths={"hermes": root}, data_dir=tmp_path / "d")
    text = db.execute("SELECT text FROM events WHERE source_id = 's1:m1:text'").fetchone()[0]
    assert FAKE_TOKEN not in text and "abcdef123456" not in text and "[REDACTED" in text
    assert search(db, FAKE_TOKEN, include_all=True) == []
    assert search(db, "hello", include_all=True)


@pytest.mark.parametrize(
    "ddl",
    [
        "CREATE TABLE schema_version (version INTEGER); CREATE TABLE sessions (id TEXT);"
        " CREATE TABLE messages (id INTEGER);",
        "CREATE TABLE sessions (id TEXT, started_at REAL);",
    ],
)
def test_unknown_schema_fails_loud(tmp_path, ddl):
    connector = HermesConnector(tmp_path / "hermes")
    (tmp_path / "hermes").mkdir()
    db = tmp_path / "hermes" / "state.db"
    c = sqlite3.connect(db)
    c.executescript(ddl)
    c.commit()
    c.close()
    with pytest.raises(ConnectorError, match="schema unrecognized"):
        connector.check_version(db)


@pytest.mark.parametrize(
    "rows",
    [
        "",
        "INSERT INTO schema_version VALUES ('v2');",
        "INSERT INTO schema_version VALUES (1); INSERT INTO schema_version VALUES (2);",
    ],
)
def test_schema_version_must_be_one_integer(tmp_path, rows):
    connector = HermesConnector(tmp_path / "hermes")
    db = build_hermes_db(tmp_path / "hermes" / "state.db")
    c = sqlite3.connect(db)
    c.execute("DELETE FROM schema_version")
    c.executescript(rows)
    c.commit()
    c.close()
    with pytest.raises(ConnectorError, match="schema_version"):
        connector.check_version(db)


def test_version_marker_and_missing(conn_root, tmp_path):
    connector, root = conn_root
    assert connector.check_version(root) == "hermes-sqlite-v1:schema-32"
    with pytest.raises(ConnectorError, match="not found"):
        connector.check_version(tmp_path / "hermes" / "nothing")
    (tmp_path / "hermes" / "empty").mkdir()
    with pytest.raises(ConnectorError, match="no state.db"):
        connector.check_version(tmp_path / "hermes" / "empty")


def test_denylist_and_scope_enforced(conn_root, tmp_path):
    connector, _ = conn_root
    for name in (".env", ".env.local", "auth.json", "config.yaml", ".hermes_history", "kanban.db"):
        with pytest.raises(DenylistViolation):
            connector.check_version(connector.source_root / name)
    with pytest.raises(ConnectorError, match="outside"):
        connector.check_version(tmp_path / "elsewhere")


def test_only_state_dbs_are_opened(conn_root, tmp_path, monkeypatch):
    connector, root = conn_root
    opened = []
    real = sqlite3.connect

    def spy(target, *a, **k):
        opened.append(str(target))
        return real(target, *a, **k)

    monkeypatch.setattr(sqlite3, "connect", spy)
    fetch(connector, root, tmp_path)
    monkeypatch.undo()
    source_opens = [o for o in opened if str(root) in o]
    assert source_opens and all("state.db" in o for o in source_opens)


# ---- WAL handling ----------------------------------------------------------------------
def test_wal_database_closed_cleanly_is_readable(tmp_path):
    connector = HermesConnector(tmp_path / "hermes")
    root = build_hermes_home(tmp_path / "hermes", wal=True)
    assert not (root / "state.db-wal").exists() or (root / "state.db-wal").stat().st_size == 0
    sessions, events = split(fetch(connector, root, tmp_path)[0])
    assert "s1" in sessions and "s1:m1:text" in events


def test_uncheckpointed_wal_rows_are_imported(tmp_path):
    connector = HermesConnector(tmp_path / "hermes")
    db = build_hermes_db(tmp_path / "hermes" / "state.db", wal=True)
    writer = sqlite3.connect(db)  # a running Hermes: holds the DB open, WAL not checkpointed
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute(
        "INSERT INTO messages(session_id, role, content, timestamp) VALUES ('s1','user',"
        "'only in the wal quokka', 1790849999.0)"
    )
    writer.commit()
    assert (tmp_path / "hermes" / "state.db-wal").stat().st_size > 0
    try:
        _, events = split(fetch(connector, db, tmp_path)[0])
    finally:
        writer.close()
    assert any("only in the wal" in e.text for e in events.values())


def _fail_ro(monkeypatch):
    real = sqlite3.connect

    def connect(target, *a, **k):
        if isinstance(target, str) and "mode=ro" in target:
            raise sqlite3.OperationalError("unable to open database file")
        return real(target, *a, **k)

    monkeypatch.setattr(sqlite_common.sqlite3, "connect", connect)


def test_ro_open_failure_falls_back_to_immutable_when_wal_empty(tmp_path, monkeypatch):
    db = build_hermes_db(tmp_path / "state.db")
    _fail_ro(monkeypatch)
    conn = sqlite_common.connect_readonly(db)
    assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 2
    conn.close()


def test_ro_open_failure_with_live_wal_raises(tmp_path, monkeypatch):
    db = build_hermes_db(tmp_path / "state.db")
    (tmp_path / "state.db-wal").write_bytes(b"x" * 64)
    _fail_ro(monkeypatch)
    with pytest.raises(ConnectorError, match="uncheckpointed WAL"):
        sqlite_common.connect_readonly(db)


# ---- paths / env / registry -------------------------------------------------------------
def test_default_home_resolution(tmp_path):
    home = tmp_path / "home"
    assert default_home({}, "linux", home) == home / ".hermes"
    assert default_home({}, "darwin", home) == home / ".hermes"
    assert default_home({"HERMES_HOME": str(tmp_path / "h")}, "linux", home) == tmp_path / "h"
    assert default_home({"LOCALAPPDATA": "C:/L"}, "win32", home).name == "hermes"
    assert default_home({}, "win32", home) == home / "AppData" / "Local" / "hermes"
    assert (
        default_home({"HERMES_HOME": "/x", "LOCALAPPDATA": "C:/L"}, "win32", home).as_posix()
        == "/x"
    )
    c = HermesConnector(env={"HERMES_HOME": str(tmp_path / "custom")})
    assert c.default_source_path() == tmp_path / "custom"
    assert c.allowed_paths == [tmp_path / "custom"]


def test_registry_override_beats_hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "from_env"))
    monkeypatch.setenv("CTX_SOURCE_ROOT_HERMES", str(tmp_path / "override"))
    assert available_connectors()["hermes"].default_source_path() == tmp_path / "override"
    monkeypatch.delenv("CTX_SOURCE_ROOT_HERMES")
    assert available_connectors()["hermes"].default_source_path() == tmp_path / "from_env"


def test_dry_run_report_writes_nothing(conn_root, tmp_path):
    connector, root = conn_root
    before = sorted(p.name for p in tmp_path.rglob("*"))
    report = dry_run_report(connector, root)
    assert report.sessions == 4 and report.events > 20
    assert report.stats.redaction_matches_by_kind and report.stats.oversized_events == 2
    assert sorted(p.name for p in tmp_path.rglob("*")) == before


def test_cli_sync_source(tmp_path, monkeypatch):
    runner = CliRunner()
    build_hermes_home(tmp_path / "hm")
    monkeypatch.setenv("CTX_SOURCE_ROOT_HERMES", str(tmp_path / "hm"))
    home = tmp_path / "ctx_home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "projects.toml").write_text(PROJECTS_TOML)
    dry = runner.invoke(app, ["sync", "--dry-run", "--source", "hermes"])
    assert dry.exit_code == 0, dry.output
    assert "[dry-run]" in dry.output and not (home / "ctx.db").exists()
    first = runner.invoke(app, ["sync", "--source", "hermes"])
    assert first.exit_code == 0, first.output
    assert "hermes-sqlite-v1" in first.output and "sessions 4" in first.output
    second = runner.invoke(app, ["sync", "--source", "hermes"])
    assert "added 0," in second.output
    out = runner.invoke(app, ["search", "zebra", "--all"]).stdout
    assert "hermes session=" in out and FAKE_TOKEN not in out
    shown = runner.invoke(app, ["sources", "show", "hermes"]).output
    assert "auth.json" in shown and "state_meta" in shown
