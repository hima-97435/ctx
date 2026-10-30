import sqlite3
from collections import Counter
from dataclasses import asdict

import pytest

from ctx.connectors import opencode as oc
from ctx.connectors.base import ConnectorError, DenylistViolation, assert_tables_allowed
from ctx.core.blobs import BlobStore
from tests.fixtures.opencode_db import build_opencode_db as _fixture_db


@pytest.fixture()
def fixture_db(tmp_path):
    return _fixture_db(tmp_path / "opencode.db")


@pytest.fixture()
def ro_conn(fixture_db):
    conn = oc.open_readonly(
        fixture_db, source_root=fixture_db.parent, denylist_paths=("auth.json",)
    )
    yield conn
    conn.close()


def test_check_version_ok_and_marker(ro_conn):
    version = oc.check_version(ro_conn)
    assert version == "drizzle:202604230708"  # hash is NULL in real DBs; name is used


def test_check_version_fails_loud_on_unknown_schema(tmp_path):
    path = tmp_path / "opencode.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE __drizzle_migrations (id INTEGER PRIMARY KEY, hash TEXT);
        CREATE TABLE session (id TEXT PRIMARY KEY, directory TEXT);
        CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, data TEXT);
        CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT);
        """
    )
    conn.commit()
    ro = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    ro.row_factory = sqlite3.Row
    with pytest.raises(ConnectorError, match="unrecognized"):
        oc.check_version(ro)
    ro.close()


def test_denylist_table_assertion():
    with pytest.raises(DenylistViolation):
        assert_tables_allowed("SELECT * FROM credential", oc.DENYLIST_TABLES)
    # the statements the connector actually runs must pass its own denylist
    for sql in (
        "SELECT count(*) FROM session",
        "SELECT id, json_extract(data, '$.role') AS role FROM message",
        "SELECT id, message_id, data FROM part",
    ):
        assert_tables_allowed(sql, oc.DENYLIST_TABLES)


def test_path_denylist_blocks_auth_json(tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text("{}")
    with pytest.raises(DenylistViolation, match="auth.json"):
        oc.open_readonly(auth, source_root=tmp_path, denylist_paths=("auth.json",))


def test_path_guard_blocks_outside_root(tmp_path):
    outside = tmp_path.parent / "elsewhere.db"
    with pytest.raises(ConnectorError, match="outside allowed root"):
        oc.open_readonly(outside, source_root=tmp_path, denylist_paths=())


def test_iter_sessions_maps_fields(ro_conn):
    sessions = list(oc.iter_sessions(ro_conn))
    assert len(sessions) == 1
    s = sessions[0]
    assert s.source == "opencode"
    assert s.source_session_id == "ses_1"
    assert s.cwd == "/tmp/proj"
    assert s.model == "nvidia/glm-5.3-flash"
    assert s.agent == "build"
    assert s.started_at == 1000 and s.ended_at == 2000


def test_iter_events_extraction(tmp_path, ro_conn):
    store = BlobStore(tmp_path / "blobs")
    events = {e.source_id: e for e in oc.iter_events(ro_conn, blob_store=store)}

    # step-start skipped; order by (time_created, id)
    assert list(events) == ["prt_1", "prt_2", "prt_4", "prt_5", "prt_6", "prt_7"]

    text_event = events["prt_1"]
    assert text_event.kind == "text" and text_event.role == "user"
    assert "api_key=abcdef123456" not in text_event.text
    assert "[REDACTED:secret]" in text_event.text
    assert text_event.truncated is False and text_event.blob_ref is None
    assert text_event.token_estimate == len(text_event.text) // 4
    assert len(text_event.content_hash) == 64

    bash_event = events["prt_2"]
    assert bash_event.kind == "tool:bash" and bash_event.call_id == "c1"
    assert bash_event.role == "assistant"
    assert "ls" in bash_event.text and "file1" in bash_event.text

    read_event = events["prt_4"]
    assert read_event.kind == "tool:read"
    assert read_event.truncated is True and read_event.blob_ref is not None
    blob = store.read(read_event.blob_ref)
    assert "deepsecretvalue123" not in blob
    assert "[REDACTED:secret]" in blob
    assert "deepsecretvalue123" not in read_event.text
    assert "truncated" in read_event.text and read_event.blob_ref in read_event.text
    assert store.exists(read_event.blob_ref)  # blob-before-event

    compaction = events["prt_5"]
    assert compaction.kind == "compaction" and compaction.synthetic is True

    file_event = events["prt_6"]
    assert file_event.kind == "file" and "a.ipynb" in file_event.text


def test_sensitive_read_output_redacted(tmp_path, ro_conn):
    store = BlobStore(tmp_path / "blobs")
    events = {e.source_id: e for e in oc.iter_events(ro_conn, blob_store=store)}
    env_event = events["prt_7"]
    assert env_event.kind == "tool:read"
    assert "abcdefgh123" not in env_event.text
    assert "postgres://u:p@h/db" not in env_event.text
    assert "[REDACTED:env-value]" in env_event.text
    assert "SECRET_TOKEN=" in env_event.text  # key names in .env are kept


def test_dry_run_report_counts(tmp_path):
    fixture = _fixture_db(tmp_path / "opencode.db")
    report = oc.dry_run_report(fixture, source_root=tmp_path, denylist_paths=("auth.json",))
    assert report.schema_version == "drizzle:202604230708"
    assert (report.sessions, report.messages, report.parts) == (1, 2, 7)
    assert report.skipped_parts == 1  # step-start
    assert report.parts_by_type["tool"] == 3
    assert report.parts_by_tool["read"] == 2
    assert report.parts_by_tool["bash"] == 1
    assert report.roles["user"] == 1 and report.roles["assistant"] == 5
    assert report.redaction_matches_by_kind["secret"] == 2  # api_key (prt_1) + deep secret (prt_4)
    assert report.redaction_matches_by_kind["env-value"] == 2  # prt_7 .env output
    assert report.parts_with_redaction_by_type == Counter({"tool": 2, "text": 1})
    assert report.oversized_parts == 1
    assert report.oversized_content_chars > 64 * 1024
    blob_ref_free = asdict(report)
    assert "CANARY-SUPERSECRET" not in str(blob_ref_free)
