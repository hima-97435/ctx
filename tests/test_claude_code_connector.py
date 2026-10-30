import json

import pytest
from typer.testing import CliRunner

from ctx.cli.main import app
from ctx.connectors.base import ConnectorError, DenylistViolation
from ctx.connectors.claude_code import ClaudeCodeConnector
from ctx.connectors.jsonl_common import dry_run_report
from ctx.core.blobs import BlobStore
from ctx.core.db import connect
from ctx.core.model import RawEvent, RawSession
from ctx.core.search import search
from ctx.core.sync import sync
from tests.fixtures.claude_code_jsonl import (
    FAKE_TOKEN,
    SESSION,
    SESSION_2,
    assistant,
    build_claude_root,
    main_lines,
    user_text,
    write_jsonl,
)
from tests.helpers import PROJECTS_TOML


@pytest.fixture()
def conn_root(tmp_path):
    connector = ClaudeCodeConnector(tmp_path / "cc")
    return connector, build_claude_root(tmp_path / "cc")


def fetch(connector, path, tmp_path):
    store = BlobStore(tmp_path / "blobs")
    return list(connector.fetch(path, blob_store=store, tmp_dir=tmp_path / "t")), store


def test_sessions_and_mapping(conn_root, tmp_path):
    connector, path = conn_root
    records, store = fetch(connector, path, tmp_path)
    sessions = {r.source_session_id: r for r in records if isinstance(r, RawSession)}
    events = {r.source_id: r for r in records if isinstance(r, RawEvent)}
    assert set(sessions) == {SESSION, SESSION_2}
    s = sessions[SESSION]
    assert s.cwd == "/tmp/proj" and s.model == "claude-test-1" and s.title == "Zebra migration"
    assert s.started_at == 1_790_848_800_000 and s.ended_at > s.started_at
    assert sessions[SESSION_2].cwd == "/tmp/proj/sub/deep"

    assert events["u1:0"].role == "user" and events["u1:0"].kind == "text"
    assert events["u1:0"].message_ref == "u1" and events["u1:0"].ts == s.started_at
    assert events["a1:0"].kind == "reasoning" and events["a1:0"].role == "assistant"
    assert events["a2:0"].kind == "text" and events["a2:1"].kind == "tool:Bash"
    assert events["a2:1"].call_id == "toolu_1" and '"command": "ls"' in events["a2:1"].text
    assert events["u2:0"].kind == "tool_result" and events["u2:0"].call_id == "toolu_1"
    assert events["u2:0"].text == "file1\nfile2" and events["u2:0"].role == "user"
    assert events["u3:0"].kind == "tool_result" and "PLAIN=[REDACTED" in events["u3:0"].text
    assert events["u5:0"].text == "thanks"  # text block list form
    assert events["m1:0"].synthetic is True and events["u1:0"].synthetic is False
    assert events["a5:0"].text == "done"


def test_large_output_spills_to_redacted_blob(conn_root, tmp_path):
    connector, path = conn_root
    records, store = fetch(connector, path, tmp_path)
    big = next(r for r in records if isinstance(r, RawEvent) and r.source_id == "u4:0")
    assert big.truncated and big.blob_ref and store.exists(big.blob_ref)
    blob = store.read(big.blob_ref)
    assert "deepsecretvalue123" not in blob and "[REDACTED" in blob
    assert len(big.text) < 10_000


def test_skips_are_counted_not_fatal(conn_root, tmp_path):
    connector, path = conn_root
    records, _ = fetch(connector, path, tmp_path)
    ids = {r.source_id for r in records if isinstance(r, RawEvent)}
    assert not any(i.startswith("s1:") for i in ids)  # sidechain
    st = connector.last_stats
    assert st.skipped["sidechain"] == 1
    assert st.skipped["bookkeeping:mode"] == 1 and st.skipped["bookkeeping:summary"] == 1
    assert st.skipped["unknown_type:totally-new-type"] == 1
    assert st.skipped["unknown_block:mystery_block"] == 1
    assert st.skipped["empty_block"] >= 0
    assert st.malformed_lines == 2  # garbage line + truncated final line
    assert st.files == 2


def test_resumed_session_duplicates_collapse(tmp_path):
    connector = ClaudeCodeConnector(tmp_path / "cc")
    projects = build_claude_root(tmp_path / "cc")
    write_jsonl(projects / "-tmp-proj" / "resumed.jsonl", main_lines()[1:3])
    records, _ = fetch(connector, projects, tmp_path)
    ids = [r.source_id for r in records if isinstance(r, RawEvent)]
    assert len(ids) == len(set(ids))
    assert connector.last_stats.skipped["duplicate_in_run"] >= 2


def test_incremental_append_adds_only_new_events(tmp_path):
    connector = ClaudeCodeConnector(tmp_path / "cc")
    projects = build_claude_root(tmp_path / "cc")
    db = connect(tmp_path / "ctx.db")
    kw = {"source_paths": {"claude_code": projects}, "data_dir": tmp_path / "data"}
    first = sync(db, {"claude_code": connector}, **kw)[0]
    assert first.status == "ok"
    f = projects / "-tmp-proj" / f"{SESSION}.jsonl"
    with f.open("a") as fh:
        fh.write("\n")  # the fixture ends in a torn line; a live writer finishes it
        fh.write(json.dumps(user_text("u9", "u5", "2026-10-01T11:00:00Z", "later quokka")) + "\n")
        fh.write(
            json.dumps(
                assistant("a9", "u9", "2026-10-01T11:00:01Z", [{"type": "text", "text": "ack"}])
            )
            + "\n"
        )
    second = sync(db, {"claude_code": connector}, **kw)[0]
    assert second.events_new == 2 and second.events_duplicate < first.events_new
    assert second.sessions_new == 0
    end = db.execute(
        "SELECT ended_at FROM sessions WHERE source_session_id = ?", (SESSION,)
    ).fetchone()
    assert end[0] == 1_790_852_401_000  # session end moved with the append
    assert search(db, "quokka", include_all=True)


def test_project_linking_via_cwd(tmp_path):
    connector = ClaudeCodeConnector(tmp_path / "cc")
    projects = build_claude_root(tmp_path / "cc")
    db = connect(tmp_path / "ctx.db")
    cfg = tmp_path / "projects.toml"
    cfg.write_text(PROJECTS_TOML)
    from ctx.core.projects import load_projects_config

    sync(
        db,
        {"claude_code": connector},
        source_paths={"claude_code": projects},
        project_configs=load_projects_config(cfg),
        data_dir=tmp_path / "data",
    )
    rows = {r[0]: r[1] for r in db.execute("SELECT source_session_id, project_id FROM sessions")}
    assert rows[SESSION] == "proj" and rows[SESSION_2] == "sub"


def test_fake_token_redacted_in_fts(conn_root, tmp_path):
    connector, path = conn_root
    db = connect(tmp_path / "ctx.db")
    sync(
        db, {"claude_code": connector}, source_paths={"claude_code": path}, data_dir=tmp_path / "d"
    )
    text = db.execute("SELECT text FROM events WHERE source_id = 'u1:0'").fetchone()[0]
    assert FAKE_TOKEN not in text and "abcdef123456" not in text and "[REDACTED" in text
    assert search(db, FAKE_TOKEN, include_all=True) == []
    assert search(db, "hello", include_all=True)


def test_unknown_major_shape_fails_loud(tmp_path):
    connector = ClaudeCodeConnector(tmp_path / "cc")
    projects = tmp_path / "cc" / "projects"
    for name, lines in {
        "array": ["[1, 2]"],
        "garbage": ["nope", "still nope"],
        "nouuid": [
            json.dumps(
                {
                    "type": "user",
                    "sessionId": "s",
                    "timestamp": "2026-10-01T00:00:00Z",
                    "message": {"role": "user", "content": "x"},
                }
            )
        ],
        "notime": [
            json.dumps(
                {
                    "type": "assistant",
                    "uuid": "u",
                    "sessionId": "s",
                    "message": {"role": "assistant", "content": []},
                }
            )
        ],
    }.items():
        d = projects / name
        write_jsonl(d / "s.jsonl", lines)
        with pytest.raises(ConnectorError):
            connector.check_version(d)


def test_version_marker_and_empty_dir(conn_root, tmp_path):
    connector, path = conn_root
    assert connector.check_version(path) == "claude-code-jsonl-v1:cli-2.1.0"
    empty = tmp_path / "cc" / "projects2"
    empty.mkdir()
    assert connector.check_version(empty).endswith(":empty")
    with pytest.raises(ConnectorError, match="not found"):
        connector.check_version(tmp_path / "cc" / "missing")


def test_denylist_and_scope_enforced(conn_root, tmp_path):
    connector, path = conn_root
    root = connector.source_root
    for name in (".credentials.json", "settings.json", "settings.local.json", ".claude.json"):
        with pytest.raises(DenylistViolation):
            connector.check_version(root / name)
    with pytest.raises(ConnectorError, match="outside"):
        connector.check_version(tmp_path / "elsewhere")
    # only projects/*/*.jsonl is ever opened; planted denylisted files are not read
    opened = []
    real_open = type(path).open

    def spy(self, *a, **k):
        opened.append(self.name)
        return real_open(self, *a, **k)

    import pathlib

    pathlib.Path.open = spy
    try:
        fetch(connector, path, tmp_path)
    finally:
        pathlib.Path.open = real_open
    assert set(opened) <= {f"{SESSION}.jsonl", f"{SESSION_2}.jsonl"}


def test_dry_run_report_writes_nothing(conn_root, tmp_path):
    connector, path = conn_root
    before = sorted(p.name for p in tmp_path.rglob("*"))
    report = dry_run_report(connector, path)
    assert report.sessions == 2 and report.events > 10
    assert report.stats.redaction_matches_by_kind and report.stats.oversized_events == 1
    assert sorted(p.name for p in tmp_path.rglob("*")) == before


def test_cli_sync_source_override(tmp_path, monkeypatch):
    runner = CliRunner()
    projects = build_claude_root(tmp_path / "cc")
    assert projects.exists()
    monkeypatch.setenv("CTX_SOURCE_ROOT_CLAUDE_CODE", str(tmp_path / "cc"))
    home = tmp_path / "ctx_home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "projects.toml").write_text(PROJECTS_TOML)
    dry = runner.invoke(app, ["sync", "--dry-run", "--source", "claude_code"])
    assert dry.exit_code == 0, dry.output
    assert "[dry-run]" in dry.output and not (home / "ctx.db").exists()
    first = runner.invoke(app, ["sync", "--source", "claude_code"])
    assert first.exit_code == 0, first.output
    assert "claude-code-jsonl-v1" in first.output and " 2 " in first.output
    second = runner.invoke(app, ["sync", "--source", "claude_code"])
    assert second.exit_code == 0 and "ERROR" not in second.output
    out = runner.invoke(app, ["search", "zebra", "--all"]).stdout
    assert "claude_code session=" in out and FAKE_TOKEN not in out
    assert "claude_code" in runner.invoke(app, ["sources", "list"]).output
    shown = runner.invoke(app, ["sources", "show", "claude_code"]).output
    assert ".credentials.json" in shown
