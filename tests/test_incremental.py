"""Incremental sync state (ADR-024), absent-source handling and session merging."""

import json
import os
import sqlite3

import pytest

from ctx.connectors.base import ConnectorError
from ctx.connectors.claude_code import ClaudeCodeConnector
from ctx.connectors.hermes import HermesConnector
from ctx.core.db import connect
from ctx.core.model import RawSession
from ctx.core.sync import dry_run, sync
from ctx.core.sync_state import SyncState, fingerprint
from tests.fixtures.claude_code_jsonl import (
    SESSION,
    SESSION_2,
    assistant,
    build_claude_root,
    main_lines,
    user_text,
    write_jsonl,
)
from tests.fixtures.hermes_db import build_hermes_db
from tests.helpers import FakeConnector, raw_event


def count(db, table="events"):
    return db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


@pytest.fixture()
def cc(tmp_path):
    connector = ClaudeCodeConnector(tmp_path / "cc")
    projects = build_claude_root(tmp_path / "cc")
    db = connect(tmp_path / "ctx.db")
    kw = {"source_paths": {"claude_code": projects}, "data_dir": tmp_path / "data"}

    def run(**extra):
        return sync(db, {"claude_code": connector}, **kw, **extra)[0]

    return projects, db, run


def test_state_rows_written_per_file_and_unchanged_skipped(cc):
    projects, db, run = cc
    first = run()
    assert first.unchanged_units == 0
    keys = [r[0] for r in db.execute("SELECT key FROM sync_state WHERE source='claude_code'")]
    assert len(keys) == 2 and all(k.startswith("file:") for k in keys)
    second = run()
    assert second.unchanged_units == 2 and second.events_seen == 0 and second.sessions_seen == 0
    assert count(db) == first.events_new


def test_only_the_changed_file_is_reread(cc):
    projects, db, run = cc
    first = run()
    f = projects / "-tmp-proj" / f"{SESSION}.jsonl"
    with f.open("a") as fh:
        fh.write("\n")
        fh.write(json.dumps(user_text("u9", "u5", "2026-10-01T11:00:00Z", "later quokka")) + "\n")
    second = run()
    assert second.unchanged_units == 1 and second.events_new == 1
    assert second.events_seen < first.events_seen
    third = run()
    assert third.unchanged_units == 2 and third.events_new == 0


def test_truncated_file_is_rescanned_with_no_duplicates(cc):
    projects, db, run = cc
    first = run()
    f = projects / "-tmp-proj" / f"{SESSION}.jsonl"
    before = f.stat().st_size
    lines = main_lines()[:3]
    write_jsonl(f, lines)  # rewritten shorter: size shrinks
    assert f.stat().st_size < before
    second = run()
    assert second.unchanged_units == 1  # the other file is untouched
    assert second.events_seen > 0 and second.events_new == 0  # rescanned, nothing new
    assert count(db) == first.events_new  # append-only: truncation never deletes events


def test_rewrite_with_new_content_is_picked_up_even_if_smaller(cc):
    projects, db, run = cc
    run()
    f = projects / "-tmp-proj" / f"{SESSION}.jsonl"
    new = [
        user_text("n1", None, "2026-10-02T09:00:00Z", "fresh start"),
        assistant("n2", "n1", "2026-10-02T09:00:01Z", [{"type": "text", "text": "narwhal"}]),
    ]
    write_jsonl(f, new)
    second = run()
    assert second.events_new == 2
    assert db.execute("SELECT count(*) FROM events WHERE text='narwhal'").fetchone()[0] == 1


def test_same_size_rewrite_detected_by_mtime(cc):
    projects, db, run = cc
    run()
    f = projects / "-tmp-proj-sub" / f"{SESSION_2}.jsonl"
    size = f.stat().st_size
    f.write_bytes(f.read_bytes())  # identical bytes, new mtime
    os.utime(f, ns=(1, f.stat().st_mtime_ns + 5_000_000))
    assert f.stat().st_size == size
    second = run()
    assert second.unchanged_units == 1 and second.events_new == 0


def test_state_deleted_means_full_rescan_zero_duplicates(cc):
    projects, db, run = cc
    first = run()
    ids = [tuple(r) for r in db.execute("SELECT id, source_id FROM events ORDER BY id")]
    db.execute("DELETE FROM sync_state")
    second = run()
    assert second.unchanged_units == 0 and second.events_new == 0
    assert second.events_duplicate == first.events_new
    assert [tuple(r) for r in db.execute("SELECT id, source_id FROM events ORDER BY id")] == ids
    assert count(db, "sync_state") == 2  # re-recorded


def test_dropped_sync_state_table_is_recreated_by_migration_semantics(cc):
    projects, db, run = cc
    run()
    db.execute("DELETE FROM sync_state")
    assert run(full=True).events_new == 0
    assert count(db, "sync_state") == 2


def test_corrupt_state_value_is_treated_as_no_state(cc):
    projects, db, run = cc
    first = run()
    db.execute("UPDATE sync_state SET value = 'not json{'")
    second = run()
    assert second.unchanged_units == 0 and second.events_duplicate == first.events_new
    assert second.events_new == 0


def test_full_flag_ignores_state(cc):
    projects, db, run = cc
    first = run()
    full = run(full=True)
    assert full.unchanged_units == 0 and full.events_duplicate == first.events_new


def test_failed_run_records_no_state(tmp_path):
    class Boom(FakeConnector):
        def fetch(self, source_path, *, blob_store, tmp_dir, state=None):
            yield from super().fetch(source_path, blob_store=blob_store, tmp_dir=tmp_dir)
            state.stage("unit", [1, 2])
            raise ConnectorError("boom")

    fake = Boom(tmp_path / "fr", [raw_event("e1", "hello")])
    fake.root.mkdir()
    db = connect(tmp_path / "x.db")
    r = sync(db, {"fake": fake}, data_dir=tmp_path / "d")[0]
    assert r.status == "error"
    assert count(db, "sync_state") == 0 and count(db) == 0  # rolled back together


def test_connector_without_state_param_still_works(tmp_path):
    class Old(FakeConnector):
        def fetch(self, source_path, *, blob_store, tmp_dir):
            yield from super().fetch(source_path, blob_store=blob_store, tmp_dir=tmp_dir)

    fake = Old(tmp_path / "fr", [raw_event("e1", "hello")])
    fake.root.mkdir()
    db = connect(tmp_path / "x.db")
    assert sync(db, {"fake": fake}, data_dir=tmp_path / "d")[0].events_new == 1


def test_fingerprint_and_syncstate_unit(tmp_path):
    f = tmp_path / "a"
    f.write_text("x")
    fp = fingerprint(f, tmp_path / "missing")
    assert fp[0][0] == 1 and fp[1] is None
    state = SyncState()
    state.stage("k", fp)
    assert not state.is_unchanged("k", fp) and state.unchanged == 0


# --- sqlite sources ---------------------------------------------------------------------


def test_hermes_only_changed_database_is_reread(tmp_path):
    root = tmp_path / "hermes"
    build_hermes_db(root / "state.db")
    build_hermes_db(root / "profiles" / "work" / "state.db")
    connector = HermesConnector(root)
    db = connect(tmp_path / "ctx.db")
    kw = {"source_paths": {"hermes": root}, "data_dir": tmp_path / "data"}
    first = sync(db, {"hermes": connector}, **kw)[0]
    assert first.status == "ok" and first.unchanged_units == 0
    c = sqlite3.connect(root / "profiles" / "work" / "state.db")
    c.execute(
        "INSERT INTO messages(session_id, role, content, timestamp)"
        " VALUES ('s1','user','later quokka', 1790849999.0)"
    )
    c.commit()
    c.close()
    second = sync(db, {"hermes": connector}, **kw)[0]
    assert second.unchanged_units == 1 and second.events_new == 1
    assert second.events_seen < first.events_seen


def test_wal_only_change_is_detected(tmp_path):
    root = tmp_path / "hermes"
    build_hermes_db(root / "state.db")
    db_file = root / "state.db"
    base = fingerprint(db_file, db_file.with_name("state.db-wal"))
    wal = db_file.with_name("state.db-wal")
    wal.write_bytes(b"")
    assert fingerprint(db_file, wal) != base  # -wal appearing changes the cursor


# --- sessions are merged, never erased by partial (incremental) scans --------------------


def test_partial_session_does_not_erase_cwd_title_or_end(tmp_path):
    class Seq(FakeConnector):
        def __init__(self, root):
            super().__init__(root, [])
            self.round = 0

        def fetch(self, source_path, *, blob_store, tmp_dir, state=None):
            if self.round == 0:
                yield RawSession("fake", "s1", 1000, 9000, "/tmp/proj", "m", "a", "Title")
            else:
                yield RawSession("fake", "s1", 1000, 5000, None, None, None, None)

    fake = Seq(tmp_path / "fr")
    fake.root.mkdir()
    db = connect(tmp_path / "x.db")
    from ctx.core.projects import ProjectConfig

    cfg = [ProjectConfig(id="p", name="P", allowed=True, roots=["/tmp/proj"])]
    sync(db, {"fake": fake}, data_dir=tmp_path / "d", project_configs=cfg)
    fake.round = 1
    sync(db, {"fake": fake}, data_dir=tmp_path / "d", project_configs=cfg)
    row = db.execute("SELECT * FROM sessions").fetchone()
    assert (row["cwd"], row["model"], row["title"], row["ended_at"]) == (
        "/tmp/proj",
        "m",
        "Title",
        9000,
    )
    assert row["project_id"] == "p"


def test_event_links_to_session_stored_by_earlier_run(tmp_path):
    class Late(FakeConnector):
        def fetch(self, source_path, *, blob_store, tmp_dir, state=None):
            yield from self.events  # no RawSession this run

    fake = Late(tmp_path / "fr", [])
    fake.root.mkdir()
    db = connect(tmp_path / "x.db")
    db.execute(
        "INSERT INTO sessions(id, source, source_session_id, started_at) VALUES ('fake:s1',"
        " 'fake', 's1', 1)"
    )
    fake.events = [raw_event("late", "lonely event")]
    sync(db, {"fake": fake}, data_dir=tmp_path / "d")
    assert db.execute("SELECT session_id FROM events").fetchone()[0] == "fake:s1"


# --- absent / mixed sources at the engine level --------------------------------------------


def make_fakes(tmp_path):
    present = FakeConnector(tmp_path / "here", [raw_event("e1", "hello")])
    present.root.mkdir(exist_ok=True)
    absent = FakeConnector(tmp_path / "gone", [])
    absent.name = "gone"
    return {"fake": present, "gone": absent}


def test_absent_source_skipped_when_implicit(tmp_path):
    db = connect(tmp_path / "x.db")
    results = {r.source: r for r in sync(db, make_fakes(tmp_path), data_dir=tmp_path / "d")}
    assert results["fake"].status == "ok" and results["fake"].events_new == 1
    assert results["gone"].status == "not_installed" and "gone" in results["gone"].detail
    assert db.execute("SELECT count(*) FROM sync_state WHERE source='gone'").fetchone()[0] == 0


def test_absent_source_errors_when_explicit(tmp_path):
    db = connect(tmp_path / "x.db")
    r = sync(db, make_fakes(tmp_path), only=["gone"], data_dir=tmp_path / "d")[0]
    assert r.status == "error" and "not installed" in r.error


def test_explicit_mix_syncs_present_and_errors_absent(tmp_path):
    db = connect(tmp_path / "x.db")
    results = sync(db, make_fakes(tmp_path), only=["fake", "gone"], data_dir=tmp_path / "d")
    assert [r.status for r in results] == ["ok", "error"]


def test_denylisted_existing_path_still_fails_loud_alongside_absent(tmp_path):
    fakes = make_fakes(tmp_path)
    bad = tmp_path / "here" / "secret.json"
    bad.write_text("{}")
    db = connect(tmp_path / "x.db")
    results = {
        r.source: r for r in sync(db, fakes, source_paths={"fake": bad}, data_dir=tmp_path / "d")
    }
    assert results["fake"].status == "error" and "denylist" in results["fake"].error
    assert results["gone"].status == "not_installed"


def test_dry_run_marks_absent_sources(tmp_path):
    results = {r.source: r for r in dry_run(make_fakes(tmp_path), db_file=tmp_path / "none.db")}
    assert results["gone"].status == "not_installed" and results["gone"].dry_run
    assert results["fake"].events_new == 1
    err = dry_run(make_fakes(tmp_path), only=["gone"], db_file=tmp_path / "none.db")[0]
    assert err.status == "error"


def test_version_mismatch_on_existing_source_is_error_not_skip(tmp_path):
    class Pinned(FakeConnector):
        def check_version(self, source_path):
            raise ConnectorError("unrecognized format")

    p = Pinned(tmp_path / "here", [])
    p.root.mkdir()
    gone = FakeConnector(tmp_path / "gone", [])
    gone.name = "gone"
    db = connect(tmp_path / "x.db")
    results = {r.source: r for r in sync(db, {"fake": p, "gone": gone}, data_dir=tmp_path / "d")}
    assert results["fake"].status == "error" and results["gone"].status == "not_installed"
