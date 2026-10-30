import json
import sqlite3

import pytest
from typer.testing import CliRunner

from ctx.cli.main import app
from ctx.connectors.base import ConnectorError, DenylistViolation
from ctx.connectors.devin import DevinConnector, default_root
from ctx.connectors.jsonl_common import dry_run_report
from ctx.connectors.registry import available_connectors
from ctx.core.blobs import BlobStore
from ctx.core.db import connect
from ctx.core.model import RawEvent, RawSession
from ctx.core.projects import load_projects_config
from ctx.core.search import search
from ctx.core.sync import sync
from tests.fixtures.devin_db import FAKE_TOKEN, build_devin_db, build_devin_root, node_json
from tests.helpers import PROJECTS_TOML


@pytest.fixture()
def conn_db(tmp_path):
    connector = DevinConnector(tmp_path / "devin")
    return connector, build_devin_root(tmp_path / "devin")


def fetch(connector, path, tmp_path):
    store = BlobStore(tmp_path / "blobs")
    return list(connector.fetch(path, blob_store=store, tmp_dir=tmp_path / "t")), store


def split(records):
    sessions = {r.source_session_id: r for r in records if isinstance(r, RawSession)}
    events = {r.source_id: r for r in records if isinstance(r, RawEvent)}
    return sessions, events


def test_sessions_and_mapping(conn_db, tmp_path):
    connector, path = conn_db
    sessions, events = split(fetch(connector, path, tmp_path)[0])
    assert set(sessions) == {"sess-a", "sess-b", "sess-c"}
    s = sessions["sess-a"]
    assert s.cwd == "/tmp/proj" and s.model == "devin-test-1" and s.title == "Zebra migration"
    assert s.started_at == 1_790_848_800_000 and s.ended_at == 1_790_848_860_000
    assert s.agent == "normal"
    assert sessions["sess-c"].cwd is None and sessions["sess-c"].title is None

    u = events["sess-a:u1:text"]
    assert u.role == "user" and u.message_ref == "u1" and u.ts == 1_790_848_801_000
    assert events["sess-a:sys1:text"].synthetic is True and events["sess-a:sys1:text"].role is None
    assert events["sess-a:a1:thinking"].kind == "reasoning"
    assert events["sess-a:a1:text"].text == "Let me look."
    call = events["sess-a:a1:call0"]
    assert call.kind == "tool:exec" and call.call_id == "tc_1" and '"command": "ls"' in call.text
    res = events["sess-a:t1:result"]
    assert res.kind == "tool_result" and res.call_id == "tc_1" and res.text == "file1\nfile2"
    assert res.role is None
    assert "PLAIN=[REDACTED" in events["sess-a:t2:result"].text  # .env read file-redacted
    assert (
        events["sess-a:u2:image0"].kind == "file" and events["sess-a:u2:image0"].text == "[image]"
    )
    assert "IMGBYTES" not in repr(events)  # image bytes are never stored
    assert not any(k == "sess-a:a2:text" for k in events)  # empty content skipped


def test_compaction_copies_dedupe_first_node_wins(conn_db, tmp_path):
    connector, path = conn_db
    records, _ = fetch(connector, path, tmp_path)
    _, events = split(records)
    assert "REWRITTEN" not in " ".join(e.text for e in events.values())
    assert events["sess-a:u1:text"].text.startswith("hello, key is")
    st = connector.last_stats
    assert st.skipped["duplicate_node"] == 2
    # same message_id in another session is a different event
    assert "sess-b:u1:text" in events and "sess-a:u1:text" in events


def test_malformed_and_unknown_roles_are_counted(conn_db, tmp_path):
    connector, path = conn_db
    fetch(connector, path, tmp_path)
    st = connector.last_stats
    assert st.malformed_lines == 1
    assert st.skipped["unknown_role:developer"] == 1


def test_large_output_spills_to_redacted_blob(conn_db, tmp_path):
    connector, path = conn_db
    records, store = fetch(connector, path, tmp_path)
    big = next(r for r in records if isinstance(r, RawEvent) and r.source_id == "sess-a:t3:result")
    assert big.truncated and big.blob_ref and store.exists(big.blob_ref)
    blob = store.read(big.blob_ref)
    assert "deepsecretvalue123" not in blob and "[REDACTED" in blob


def test_wal_database_with_uncheckpointed_rows(tmp_path):
    connector = DevinConnector(tmp_path / "devin")
    db = build_devin_db(tmp_path / "devin" / "cli" / "sessions.db", wal=True)
    writer = sqlite3.connect(db)
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute(
        "INSERT INTO message_nodes(session_id, node_id, chat_message, created_at)"
        " VALUES ('sess-b', 50, ?, 1790849000)",
        (json.dumps(node_json("late1", "user", "only in the wal quokka", 900)),),
    )
    writer.commit()
    try:
        _, events = split(fetch(connector, db, tmp_path)[0])
    finally:
        writer.close()
    assert "sess-b:late1:text" in events


def test_incremental_append_adds_only_new_events(conn_db, tmp_path):
    connector, path = conn_db
    db = connect(tmp_path / "ctx.db")
    kw = {"source_paths": {"devin": path}, "data_dir": tmp_path / "data"}
    first = sync(db, {"devin": connector}, **kw)[0]
    assert first.status == "ok"
    c = sqlite3.connect(path)
    c.execute(
        "INSERT INTO message_nodes(session_id, node_id, chat_message, created_at)"
        " VALUES ('sess-b', 9, ?, 1790849000)",
        (json.dumps(node_json("late1", "user", "later quokka", 900)),),
    )
    c.commit()
    c.close()
    second = sync(db, {"devin": connector}, **kw)[0]
    assert second.events_new == 1 and second.events_duplicate == first.events_new
    assert search(db, "quokka", include_all=True)


def test_project_linking_via_working_directory(conn_db, tmp_path):
    connector, path = conn_db
    db = connect(tmp_path / "ctx.db")
    cfg = tmp_path / "projects.toml"
    cfg.write_text(PROJECTS_TOML)
    sync(
        db,
        {"devin": connector},
        source_paths={"devin": path},
        project_configs=load_projects_config(cfg),
        data_dir=tmp_path / "data",
    )
    rows = {r[0]: r[1] for r in db.execute("SELECT source_session_id, project_id FROM sessions")}
    assert rows["sess-a"] == "proj" and rows["sess-b"] == "sub"
    assert rows["sess-c"] not in ("proj", "sub")


def test_fake_token_redacted_in_fts(conn_db, tmp_path):
    connector, path = conn_db
    db = connect(tmp_path / "ctx.db")
    sync(db, {"devin": connector}, source_paths={"devin": path}, data_dir=tmp_path / "d")
    text = db.execute("SELECT text FROM events WHERE source_id = 'sess-a:u1:text'").fetchone()[0]
    assert FAKE_TOKEN not in text and "abcdef123456" not in text and "[REDACTED" in text
    assert search(db, FAKE_TOKEN, include_all=True) == []
    assert search(db, "hello", include_all=True)


def test_unknown_schema_fails_loud(tmp_path):
    connector = DevinConnector(tmp_path / "devin")
    d = tmp_path / "devin" / "cli"
    d.mkdir(parents=True)
    cases = {
        "missing_table": "CREATE TABLE sessions (id TEXT);",
        "missing_column": (
            "CREATE TABLE refinery_schema_history (version INT, name TEXT);"
            " CREATE TABLE sessions (id TEXT, working_directory TEXT, model TEXT, created_at INT,"
            " last_activity_at INT, title TEXT);"
            " CREATE TABLE message_nodes (session_id TEXT, node_id INT, created_at INT);"
        ),
    }
    for name, ddl in cases.items():
        db = d / f"{name}.db"
        c = sqlite3.connect(db)
        c.executescript(ddl)
        c.commit()
        c.close()
        with pytest.raises(ConnectorError, match="schema unrecognized"):
            connector.check_version(db)


@pytest.mark.parametrize(
    "bad_row",
    ["not json at all", "[1, 2]", json.dumps({"role": "user"}), json.dumps({"message_id": "m"})],
)
def test_unrecognized_chat_message_shape_fails_loud(tmp_path, bad_row):
    connector = DevinConnector(tmp_path / "devin")
    db = build_devin_db(tmp_path / "devin" / "cli" / "sessions.db")
    c = sqlite3.connect(db)
    c.execute("DELETE FROM message_nodes")
    c.execute(
        "INSERT INTO message_nodes(session_id, node_id, chat_message, created_at)"
        " VALUES ('sess-a', 1, ?, 1)",
        (bad_row,),
    )
    c.commit()
    c.close()
    with pytest.raises(ConnectorError):
        connector.check_version(db)


def test_version_marker_and_missing(conn_db, tmp_path):
    connector, path = conn_db
    assert connector.check_version(path) == "devin-sqlite-v1:refinery-20"
    assert connector.check_version(connector.source_root) == "devin-sqlite-v1:refinery-20"
    with pytest.raises(ConnectorError, match="not found"):
        connector.check_version(connector.source_root / "cli" / "none.db")


def test_denylist_and_scope_enforced(conn_db, tmp_path):
    connector, _ = conn_db
    root = connector.source_root
    for rel in ("credentials.toml", "mcp/oauth/server.json", "cli/auth.json", ".env"):
        with pytest.raises(DenylistViolation):
            connector.check_version(root / rel)
    with pytest.raises(ConnectorError, match="outside"):
        connector.check_version(tmp_path / "elsewhere.db")


def test_transcripts_and_credentials_are_never_opened(conn_db, tmp_path):
    connector, path = conn_db
    opened = []
    import pathlib

    real_open = pathlib.Path.open
    pathlib.Path.open = lambda self, *a, **k: (opened.append(self.name), real_open(self, *a, **k))[
        1
    ]
    try:
        fetch(connector, path, tmp_path)
    finally:
        pathlib.Path.open = real_open
    assert "credentials.toml" not in opened and "sess-a.json" not in opened


# ---- paths / env ------------------------------------------------------------------------
def test_default_root_per_platform(tmp_path):
    home = tmp_path / "home"
    assert default_root({}, "linux", home) == home / ".local" / "share" / "devin"
    assert default_root({}, "darwin", home) == home / ".local" / "share" / "devin"
    assert (
        default_root({"XDG_DATA_HOME": str(tmp_path / "x")}, "linux", home)
        == tmp_path / "x" / "devin"
    )
    assert default_root({"APPDATA": "C:/A"}, "win32", home).name == "devin"
    assert default_root({}, "win32", home) == home / "AppData" / "Roaming" / "devin"


def test_chisel_session_db_override(tmp_path):
    custom = tmp_path / "custom" / "my.db"
    c = DevinConnector(env={"CHISEL_SESSION_DB": str(custom)})
    assert c.default_source_path() == custom and c.allowed_paths == [custom.parent]
    build_devin_db(custom)
    assert c.check_version(c.default_source_path()).startswith("devin-sqlite-v1")
    # an explicit root (registry override) wins over the env var
    c2 = DevinConnector(tmp_path / "root", env={"CHISEL_SESSION_DB": str(custom)})
    assert c2.default_source_path() == tmp_path / "root" / "cli" / "sessions.db"


def test_registry_override(tmp_path, monkeypatch):
    monkeypatch.setenv("CHISEL_SESSION_DB", str(tmp_path / "a" / "s.db"))
    assert available_connectors()["devin"].default_source_path() == (
        tmp_path / "no_devin" / "cli" / "sessions.db"
    )  # CTX_SOURCE_ROOT_DEVIN (set by conftest) beats CHISEL_SESSION_DB
    monkeypatch.delenv("CTX_SOURCE_ROOT_DEVIN")
    assert available_connectors()["devin"].default_source_path() == tmp_path / "a" / "s.db"


def test_dry_run_report_writes_nothing(conn_db, tmp_path):
    connector, path = conn_db
    before = sorted(p.name for p in tmp_path.rglob("*"))
    report = dry_run_report(connector, path)
    assert report.sessions == 3 and report.events > 10
    assert report.stats.redaction_matches_by_kind and report.stats.oversized_events == 1
    assert sorted(p.name for p in tmp_path.rglob("*")) == before


def test_cli_sync_source(tmp_path, monkeypatch):
    runner = CliRunner()
    build_devin_root(tmp_path / "dv")
    monkeypatch.setenv("CTX_SOURCE_ROOT_DEVIN", str(tmp_path / "dv"))
    home = tmp_path / "ctx_home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "projects.toml").write_text(PROJECTS_TOML)
    dry = runner.invoke(app, ["sync", "--dry-run", "--source", "devin"])
    assert dry.exit_code == 0, dry.output
    assert "[dry-run]" in dry.output and not (home / "ctx.db").exists()
    first = runner.invoke(app, ["sync", "--source", "devin"])
    assert first.exit_code == 0, first.output
    assert "devin-sqlite-v1" in first.output and "sessions 3" in first.output
    second = runner.invoke(app, ["sync", "--source", "devin"])
    assert "added 0," in second.output
    out = runner.invoke(app, ["search", "zebra", "--all"]).stdout
    assert "devin session=" in out and FAKE_TOKEN not in out
    shown = runner.invoke(app, ["sources", "show", "devin"]).output
    assert "credentials.toml" in shown and "tool_call_state" in shown
