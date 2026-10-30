import json
import logging

import pytest
from typer.testing import CliRunner

from ctx.cli.main import app
from ctx.connectors import jsonl_common
from ctx.connectors.base import ConnectorError, DenylistViolation
from ctx.connectors.codex import CodexConnector
from ctx.connectors.jsonl_common import dry_run_report
from ctx.core.blobs import BlobStore
from ctx.core.db import connect
from ctx.core.model import RawEvent, RawSession
from ctx.core.search import search
from ctx.core.sync import sync
from tests.fixtures.codex_rollouts import (
    SID,
    SID_2,
    build_codex_root,
    msg,
    rec,
    rollout_lines,
    write_rollout,
)
from tests.helpers import PROJECTS_TOML


@pytest.fixture()
def conn_root(tmp_path):
    return CodexConnector(tmp_path / "cx"), build_codex_root(tmp_path / "cx")


def fetch(connector, path, tmp_path):
    store = BlobStore(tmp_path / "blobs")
    return list(connector.fetch(path, blob_store=store, tmp_dir=tmp_path / "t")), store


def test_sessions_and_mapping(conn_root, tmp_path):
    connector, path = conn_root
    records, store = fetch(connector, path, tmp_path)
    sessions = {r.source_session_id: r for r in records if isinstance(r, RawSession)}
    events = [r for r in records if isinstance(r, RawEvent)]
    assert set(sessions) == {SID, SID_2}
    s = sessions[SID]
    assert s.cwd == "/tmp/proj" and s.model == "gpt-test-1" and s.agent == "codex_cli_rs"
    assert s.ended_at > s.started_at
    by_kind = {}
    for e in events:
        by_kind.setdefault(e.kind, []).append(e)
    assert len(by_kind["tool:shell"]) == 3 and len(by_kind["tool_result"]) == 3
    env_ctx = next(e for e in by_kind["text"] if "environment_context" in e.text)
    assert env_ctx.synthetic is True and env_ctx.role == "user"
    hello = next(e for e in by_kind["text"] if "zebra" in e.text and e.session_ref == SID)
    assert hello.role == "user" and not hello.synthetic and "abcdef123456" not in hello.text
    assert by_kind["reasoning"][0].text == "thinking it through"
    call = by_kind["tool:shell"][0]
    assert call.call_id == "call_1" and '"ls"' in call.text
    out = next(e for e in by_kind["tool_result"] if e.call_id == "call_1")
    assert out.text == "file1\nfile2"
    envout = next(e for e in by_kind["tool_result"] if e.call_id == "call_2")
    assert "PLAIN=[REDACTED" in envout.text and "postgres://" not in envout.text
    dev = next(e for e in by_kind["text"] if e.text == "dev note")
    assert dev.role is None and dev.synthetic is True
    assert "BASE INSTRUCTIONS" not in "\n".join(e.text for e in events)
    assert all(e.source_id.startswith(e.session_ref + ":") for e in events)


def test_large_output_spills(conn_root, tmp_path):
    connector, path = conn_root
    records, store = fetch(connector, path, tmp_path)
    big = next(
        r
        for r in records
        if isinstance(r, RawEvent) and r.call_id == "call_3" and r.kind == "tool_result"
    )
    assert big.truncated and store.exists(big.blob_ref)
    assert "deepsecretvalue123" not in store.read(big.blob_ref)


def test_skips_counted(conn_root, tmp_path):
    connector, path = conn_root
    fetch(connector, path, tmp_path)
    st = connector.last_stats
    assert st.skipped["bookkeeping:event_msg"] == 1
    assert st.skipped["unknown_item:brand_new_item"] == 1
    assert st.skipped["unknown_type:future_record_type"] == 1
    assert st.malformed_lines == 2 and st.files == 2


def test_incremental_append(tmp_path):
    connector = CodexConnector(tmp_path / "cx")
    sessions = build_codex_root(tmp_path / "cx")
    db = connect(tmp_path / "ctx.db")
    kw = {"source_paths": {"codex": sessions}, "data_dir": tmp_path / "data"}
    first = sync(db, {"codex": connector}, **kw)[0]
    f = next(sessions.rglob(f"*{SID}.jsonl"))
    with f.open("a") as fh:
        fh.write("\n")  # the fixture ends in a torn line; a live writer finishes it
        fh.write(
            json.dumps(rec("2026-10-01T12:00:00Z", "response_item", msg("user", "later quokka")))
            + "\n"
        )
    second = sync(db, {"codex": connector}, **kw)[0]
    assert second.events_new == 1 and second.events_duplicate < first.events_new
    assert search(db, "quokka", include_all=True)


def test_project_linking_and_fts_redaction(conn_root, tmp_path):
    connector, path = conn_root
    db = connect(tmp_path / "ctx.db")
    cfg = tmp_path / "projects.toml"
    cfg.write_text(PROJECTS_TOML)
    from ctx.core.projects import load_projects_config

    sync(
        db,
        {"codex": connector},
        source_paths={"codex": path},
        project_configs=load_projects_config(cfg),
        data_dir=tmp_path / "d",
    )
    rows = {r[0]: r[1] for r in db.execute("SELECT source_session_id, project_id FROM sessions")}
    assert rows[SID] == "proj" and rows[SID_2] == "sub"
    assert search(db, "abcdef123456", include_all=True) == []
    assert search(db, "zebra", include_all=True)


def test_unknown_shape_fails_loud(tmp_path):
    connector = CodexConnector(tmp_path / "cx")
    sessions = tmp_path / "cx" / "sessions"
    write_rollout(sessions / "rollout-a.jsonl", [rec("t", "response_item", msg("user", "x"))])
    with pytest.raises(ConnectorError, match="session_meta"):
        connector.check_version(sessions)
    write_rollout(sessions / "rollout-a.jsonl", [rec("t", "session_meta", {"no_id": 1})])
    with pytest.raises(ConnectorError):
        connector.check_version(sessions)


def test_version_marker_empty_and_missing(conn_root, tmp_path):
    connector, path = conn_root
    assert connector.check_version(path) == "codex-rollout-v1:cli-0.99.0"
    empty = tmp_path / "cx" / "s2"
    empty.mkdir()
    assert connector.check_version(empty).endswith(":empty")
    with pytest.raises(ConnectorError, match="not found"):
        connector.check_version(tmp_path / "cx" / "nope")


def test_denylist_enforced(conn_root, tmp_path):
    connector, path = conn_root
    for name in ("auth.json", "config.toml", "state_5.sqlite"):
        with pytest.raises(DenylistViolation):
            connector.check_version(connector.source_root / name)
    with pytest.raises(ConnectorError, match="outside"):
        connector.check_version(tmp_path / "elsewhere")


def test_zst_skipped_with_warning_without_zstandard(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(jsonl_common, "zstandard_module", lambda: None)
    monkeypatch.setattr("ctx.connectors.codex.zstandard_module", lambda: None)
    connector = CodexConnector(tmp_path / "cx")
    sessions = build_codex_root(tmp_path / "cx")
    (sessions / "2026/10/03").mkdir(parents=True)
    (sessions / "2026/10/03" / "rollout-x.jsonl.zst").write_bytes(b"not really zstd")
    with caplog.at_level(logging.WARNING):
        records, _ = fetch(connector, sessions, tmp_path)
    assert connector.last_stats.skipped["zst_without_zstandard"] == 1
    assert any("zstandard" in w for w in connector.last_stats.warnings)
    assert {r.source_session_id for r in records if isinstance(r, RawSession)} == {SID, SID_2}


def test_zst_read_when_zstandard_available(tmp_path):
    zstd = pytest.importorskip("zstandard")
    connector = CodexConnector(tmp_path / "cx")
    sessions = tmp_path / "cx" / "sessions"
    body = "\n".join(json.dumps(x) for x in rollout_lines()) + "\n"
    f = sessions / "2026" / "rollout-z.jsonl.zst"
    f.parent.mkdir(parents=True)
    f.write_bytes(zstd.ZstdCompressor().compress(body.encode()))
    records, _ = fetch(connector, sessions, tmp_path)
    assert any(isinstance(r, RawEvent) and r.kind == "tool_result" for r in records)


def test_dry_run_report(conn_root, tmp_path):
    connector, path = conn_root
    report = dry_run_report(connector, path)
    assert report.sessions == 2 and report.events > 10
    assert report.stats.redaction_matches_by_kind


def test_cli_sync_source_override(tmp_path, monkeypatch):
    runner = CliRunner()
    build_codex_root(tmp_path / "cx")
    monkeypatch.setenv("CTX_SOURCE_ROOT_CODEX", str(tmp_path / "cx"))
    home = tmp_path / "ctx_home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "projects.toml").write_text(PROJECTS_TOML)
    first = runner.invoke(app, ["sync", "--source", "codex"])
    assert first.exit_code == 0, first.output
    assert "codex-rollout-v1" in first.output and " 2 " in first.output
    second = runner.invoke(app, ["sync", "--source", "codex"])
    assert second.exit_code == 0 and "ERROR" not in second.output
    out = runner.invoke(app, ["search", "zebra", "--all"]).stdout
    assert "codex session=" in out and "abcdef123456" not in out
