import json

import pytest
from typer.testing import CliRunner

from ctx.cli.main import app
from ctx.connectors.antigravity_cli import AntigravityCliConnector, file_uri_to_path
from ctx.connectors.base import ConnectorError, DenylistViolation
from ctx.connectors.jsonl_common import dry_run_report
from ctx.connectors.registry import available_connectors
from ctx.core.blobs import BlobStore
from ctx.core.db import connect
from ctx.core.model import RawEvent, RawSession
from ctx.core.projects import load_projects_config
from ctx.core.search import search
from ctx.core.sync import sync
from tests.fixtures.agy_cli import (
    CONV_1,
    CONV_2,
    CONV_LIVE,
    FAKE_TOKEN,
    build_agy_root,
    step,
    transcript_path,
    ts,
    write_jsonl,
)
from tests.helpers import PROJECTS_TOML


@pytest.fixture()
def conn_root(tmp_path):
    connector = AntigravityCliConnector(tmp_path / "agy", env={})
    return connector, build_agy_root(tmp_path / "agy")


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
    assert set(sessions) == {CONV_1, CONV_2, CONV_LIVE}
    s = sessions[CONV_1]
    assert s.title == "Zebra migration" and s.agent == "research"
    assert s.cwd == "/tmp/proj"  # from conversation_summaries.workspace_uris
    assert s.started_at == 1_790_848_800_000 and s.ended_at == 1_790_848_812_000
    assert sessions[CONV_2].cwd == "/tmp/proj/sub/deep"  # fallback: cache/last_conversations.json
    assert sessions[CONV_2].agent == "antigravity" and sessions[CONV_2].title is None

    p = f"{CONV_1}:"
    u = events[p + "0:text"]
    assert u.role == "user" and u.message_ref == f"{CONV_1}:0" and u.ts == 1_790_848_800_000
    assert events[p + "2:text"].synthetic is True and events[p + "2:text"].role is None
    assert p + "1:text" not in events  # CONVERSATION_HISTORY has no content
    assert events[p + "3:thinking"].kind == "reasoning"
    assert events[p + "3:text"].text == "Let me look." and events[p + "3:text"].role == "assistant"
    call = events[p + "3:call0"]
    assert call.kind == "tool:run_command" and '"CommandLine": "ls"' in call.text
    res = events[p + "4:result"]
    assert res.kind == "tool_result" and res.call_id == call.call_id and res.text == "file1\nfile2"
    # the .env view result is paired with its planner call and file-redacted
    assert "PLAIN=[REDACTED" in events[p + "6:result"].text
    assert events[p + "6:result"].call_id == events[p + "5:call0"].call_id
    user_cmd = events[p + "9:result"]
    assert user_cmd.call_id is None and user_cmd.kind == "tool_result"  # no planner call
    assert p + "10:text" in events  # stale RUNNING step in the middle of a file is imported
    assert events[p + "11:text"].text == "done"


def test_running_last_step_unknown_source_and_malformed_are_counted(conn_root, tmp_path):
    connector, path = conn_root
    _, events = split(fetch(connector, path, tmp_path)[0])
    assert f"{CONV_LIVE}:0:text" in events and f"{CONV_LIVE}:1:text" not in events
    st = connector.last_stats
    assert st.skipped["running_last_step"] == 1
    assert st.skipped["unknown_source:ROBOT"] == 1
    assert st.skipped["empty_marker:CONVERSATION_HISTORY"] == 1
    assert st.malformed_lines == 2  # garbage line + truncated final line
    assert st.files == 3


def test_protobuf_only_conversations_warn_but_do_not_block(conn_root, tmp_path):
    connector, path = conn_root
    records, _ = fetch(connector, path, tmp_path)
    assert records
    assert connector.last_stats.skipped["protobuf_only_conversation"] == 2  # .db + .pb
    assert any("protobuf" in w for w in connector.last_stats.warnings)


def test_full_transcript_only_and_large_output_spills(conn_root, tmp_path):
    connector, path = conn_root
    records, store = fetch(connector, path, tmp_path)
    assert "CANARY-SUPERSECRET" not in repr(records)  # truncated transcript.jsonl not read
    big = next(
        r for r in records if isinstance(r, RawEvent) and r.source_id == f"{CONV_1}:8:result"
    )
    assert big.truncated and big.blob_ref
    blob = store.read(big.blob_ref)
    assert "deepsecretvalue123" not in blob and "[REDACTED" in blob


def test_incremental_append_adds_only_new_events(conn_root, tmp_path):
    connector, root = conn_root
    db = connect(tmp_path / "ctx.db")
    kw = {"source_paths": {"antigravity_cli": root}, "data_dir": tmp_path / "data"}
    first = sync(db, {"antigravity_cli": connector}, **kw)[0]
    assert first.status == "ok"
    with transcript_path(root, CONV_2).open("a") as fh:
        fh.write(json.dumps(step(2, "USER_INPUT", "USER_EXPLICIT", content="later quokka")) + "\n")
    # the live conversation finishes: its RUNNING last step is now complete
    live = transcript_path(root, CONV_LIVE)
    lines = [json.loads(x) for x in live.read_text().splitlines()]
    lines[-1]["status"] = "DONE"
    write_jsonl(live, lines)
    second = sync(db, {"antigravity_cli": connector}, **kw)[0]
    assert second.events_new == 2 and second.events_duplicate == first.events_new
    assert second.sessions_new == 0
    assert search(db, "quokka", include_all=True)


def test_project_linking(conn_root, tmp_path):
    connector, root = conn_root
    db = connect(tmp_path / "ctx.db")
    cfg = tmp_path / "projects.toml"
    cfg.write_text(PROJECTS_TOML)
    sync(
        db,
        {"antigravity_cli": connector},
        source_paths={"antigravity_cli": root},
        project_configs=load_projects_config(cfg),
        data_dir=tmp_path / "data",
    )
    rows = {r[0]: r[1] for r in db.execute("SELECT source_session_id, project_id FROM sessions")}
    assert rows[CONV_1] == "proj" and rows[CONV_2] == "sub"
    assert rows[CONV_LIVE] not in ("proj", "sub")  # in neither cache: unlinked


def test_missing_summaries_and_caches_leave_sessions_unlinked(tmp_path):
    connector = AntigravityCliConnector(tmp_path / "agy", env={})
    root = tmp_path / "agy"
    write_jsonl(
        transcript_path(root, CONV_1), [step(0, "USER_INPUT", "USER_EXPLICIT", content="hi")]
    )
    sessions, events = split(fetch(connector, root, tmp_path)[0])
    assert sessions[CONV_1].cwd is None and sessions[CONV_1].title is None
    assert f"{CONV_1}:0:text" in events


def test_summaries_title_is_redacted(conn_root, tmp_path):
    connector, root = conn_root
    import sqlite3

    c = sqlite3.connect(root / "conversation_summaries.db")
    c.execute(
        "UPDATE conversation_summaries SET title = ? WHERE conversation_id = ?",
        (f"fix token {FAKE_TOKEN}", CONV_1),
    )
    c.commit()
    c.close()
    sessions, _ = split(fetch(connector, root, tmp_path)[0])
    assert FAKE_TOKEN not in (sessions[CONV_1].title or "")


def test_fake_token_redacted_in_fts(conn_root, tmp_path):
    connector, root = conn_root
    db = connect(tmp_path / "ctx.db")
    sync(
        db,
        {"antigravity_cli": connector},
        source_paths={"antigravity_cli": root},
        data_dir=tmp_path / "d",
    )
    text = db.execute(
        "SELECT text FROM events WHERE source_id = ?", (f"{CONV_1}:0:text",)
    ).fetchone()[0]
    assert FAKE_TOKEN not in text and "abcdef123456" not in text and "[REDACTED" in text
    assert search(db, FAKE_TOKEN, include_all=True) == []
    assert search(db, "hello", include_all=True)


def test_unknown_shape_fails_loud(tmp_path):
    connector = AntigravityCliConnector(tmp_path / "agy", env={})
    good = step(0, "USER_INPUT", "USER_EXPLICIT", content="x")
    cases = {
        "array": ["[1, 2]"],
        "no_step_index": [{k: v for k, v in good.items() if k != "step_index"}],
        "no_type": [{k: v for k, v in good.items() if k != "type"}],
        "bad_time": [{**good, "created_at": "yesterday"}],
    }
    for name, lines in cases.items():
        root = tmp_path / "agy" / name
        write_jsonl(transcript_path(root, CONV_1), lines)
        with pytest.raises(ConnectorError, match="unrecognized"):
            connector.check_version(root)


def test_summaries_db_schema_drift_fails_loud(conn_root, tmp_path):
    connector, root = conn_root
    import sqlite3

    c = sqlite3.connect(root / "conversation_summaries.db")
    c.execute("ALTER TABLE conversation_summaries RENAME COLUMN workspace_uris TO workspaces")
    c.commit()
    c.close()
    with pytest.raises(ConnectorError, match="workspace_uris"):
        connector.check_version(root)


def test_version_marker_and_missing(conn_root, tmp_path):
    connector, root = conn_root
    assert connector.check_version(root) == "antigravity-cli-transcript-v1"
    empty = tmp_path / "agy" / "empty"
    empty.mkdir()
    assert connector.check_version(empty).endswith(":empty")
    with pytest.raises(ConnectorError, match="not found"):
        connector.check_version(tmp_path / "agy" / "missing")


def test_denylist_and_scope_enforced(conn_root, tmp_path):
    connector, _ = conn_root
    root = connector.source_root
    for rel in (
        "history.jsonl",
        "settings.json",
        "config/mcp_config.json",
        "plugins/mem0/mcp_config.json",
        "oauth_creds.json",
        "auth.json",
    ):
        with pytest.raises(DenylistViolation):
            connector.check_version(root / rel)
    with pytest.raises(ConnectorError, match="outside"):
        connector.check_version(tmp_path / "elsewhere")


def test_only_transcripts_and_summaries_are_opened(conn_root, tmp_path):
    connector, path = conn_root
    opened = []
    import pathlib

    real_open = pathlib.Path.open
    real_read = pathlib.Path.read_text

    pathlib.Path.open = lambda self, *a, **k: (opened.append(self.name), real_open(self, *a, **k))[
        1
    ]
    pathlib.Path.read_text = lambda self, *a, **k: (
        opened.append(self.name),
        real_read(self, *a, **k),
    )[1]
    try:
        fetch(connector, path, tmp_path)
    finally:
        pathlib.Path.open = real_open
        pathlib.Path.read_text = real_read
    assert set(opened) <= {"transcript_full.jsonl", "last_conversations.json"}


def test_conversations_dir_env_is_honored(tmp_path):
    other = tmp_path / "elsewhere" / "convs"
    other.mkdir(parents=True)
    (other / "ffffffff-6666-4666-8666-666666666666.db").write_bytes(b"\x00")
    connector = AntigravityCliConnector(tmp_path / "agy", env={"AGY_CONVERSATIONS_DIR": str(other)})
    assert other in connector.allowed_paths
    root = tmp_path / "agy"
    write_jsonl(
        transcript_path(root, CONV_1), [step(0, "USER_INPUT", "USER_EXPLICIT", content="hi")]
    )
    fetch(connector, root, tmp_path)
    assert connector.last_stats.skipped["protobuf_only_conversation"] == 1


def test_file_uri_to_path():
    assert file_uri_to_path("file:///Users/me/my%20proj") == "/Users/me/my proj"
    assert file_uri_to_path("file:///C:/Users/me/proj") == "C:/Users/me/proj"
    assert file_uri_to_path("https://example.com/x") is None


def test_registry_and_default_root(tmp_path, monkeypatch):
    monkeypatch.setenv("CTX_SOURCE_ROOT_ANTIGRAVITY_CLI", str(tmp_path / "r"))
    assert available_connectors()["antigravity_cli"].default_source_path() == tmp_path / "r"
    monkeypatch.delenv("CTX_SOURCE_ROOT_ANTIGRAVITY_CLI")
    default = available_connectors()["antigravity_cli"].default_source_path()
    assert default.parts[-2:] == (".gemini", "antigravity-cli")


def test_dry_run_report_writes_nothing(conn_root, tmp_path):
    connector, path = conn_root
    before = sorted(p.name for p in tmp_path.rglob("*"))
    report = dry_run_report(connector, path)
    assert report.sessions == 3 and report.events > 15
    assert report.stats.redaction_matches_by_kind and report.stats.oversized_events == 1
    assert sorted(p.name for p in tmp_path.rglob("*")) == before


def test_cli_sync_source(tmp_path, monkeypatch):
    runner = CliRunner()
    build_agy_root(tmp_path / "ag")
    monkeypatch.setenv("CTX_SOURCE_ROOT_ANTIGRAVITY_CLI", str(tmp_path / "ag"))
    home = tmp_path / "ctx_home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "projects.toml").write_text(PROJECTS_TOML)
    dry = runner.invoke(app, ["sync", "--dry-run", "--source", "antigravity_cli"])
    assert dry.exit_code == 0, dry.output
    assert "[dry-run]" in dry.output and not (home / "ctx.db").exists()
    first = runner.invoke(app, ["sync", "--source", "antigravity_cli"])
    assert first.exit_code == 0, first.output
    assert "antigravity-cli-transcript-v1" in first.output and "sessions 3" in first.output
    second = runner.invoke(app, ["sync", "--source", "antigravity_cli"])
    assert "added 0," in second.output
    out = runner.invoke(app, ["search", "zebra", "--all"]).stdout
    assert "antigravity_cli session=" in out and FAKE_TOKEN not in out
    shown = runner.invoke(app, ["sources", "show", "antigravity_cli"]).output
    assert "history.jsonl" in shown and "gen_metadata" in shown


def test_ts_helper_matches_fixture():
    assert ts(61) == "2026-10-01T10:01:01Z"
