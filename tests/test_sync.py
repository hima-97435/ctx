import sqlite3

import pytest

from ctx.core.blobs import BlobStore
from ctx.core.db import connect
from ctx.core.projects import ProjectConfig, load_projects_config
from ctx.core.search import search
from ctx.core.sync import dry_run, rebuild_fts, sync
from tests.helpers import PROJECTS_TOML, FakeConnector, make_opencode_root, raw_event


@pytest.fixture()
def env(tmp_path):
    connector = make_opencode_root(tmp_path / "oc")
    cfg_path = tmp_path / "projects.toml"
    cfg_path.write_text(PROJECTS_TOML)
    conn = connect(tmp_path / "t.db")
    return connector, conn, load_projects_config(cfg_path), tmp_path / "data"


def run(env, **kw):
    connector, conn, configs, data = env
    return sync(conn, {connector.name: connector}, project_configs=configs, data_dir=data, **kw)[0]


def test_sync_imports_sessions_and_events(env):
    r = run(env)
    assert r.status == "ok"
    assert r.sessions_seen == 4 and r.sessions_new == 4
    assert r.events_seen == r.events_new == 9  # 6 fixture parts + 3 added
    conn = env[1]
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 9
    src = conn.execute("SELECT * FROM sources WHERE name='opencode'").fetchone()
    assert src["last_sync"] and src["schema_version_seen"].startswith("drizzle:")


def test_resync_zero_duplicates_and_blob_not_rewritten(env):
    first = run(env)
    second = run(env)
    assert second.events_new == 0 and second.events_duplicate == first.events_new
    assert second.sessions_new == 0
    assert env[1].execute("SELECT count(*) FROM events").fetchone()[0] == 9


def test_project_linking_longest_prefix(env):
    run(env)
    rows = {
        r["source_session_id"]: r["project_id"]
        for r in env[1].execute("SELECT source_session_id, project_id FROM sessions")
    }
    assert rows == {"ses_1": "proj", "ses_2": "sub", "ses_3": "hidden", "ses_4": None}
    ev = env[1].execute("SELECT project_id FROM events WHERE source_id='prt_ses_2'").fetchone()
    assert ev["project_id"] == "sub"


def test_relinking_after_config_change_updates_sessions_not_events(env):
    connector, conn, configs, data = env
    run(env)
    new_configs = configs + [
        ProjectConfig(id="else", name="Else", allowed=True, roots=["/somewhere"])
    ]
    sync(conn, {connector.name: connector}, project_configs=new_configs, data_dir=data)
    assert (
        conn.execute("SELECT project_id FROM sessions WHERE source_session_id='ses_4'").fetchone()[
            0
        ]
        == "else"
    )
    # raw event rows are append-only: their stored link is untouched...
    assert (
        conn.execute("SELECT project_id FROM events WHERE source_id='prt_ses_4'").fetchone()[0]
        is None
    )
    # ...but search follows the session link
    hits = search(conn, "zebra", project="else")
    assert [h.citation.source_session_id for h in hits] == ["ses_4"]


def test_fts_updated_on_sync_and_rebuildable(env):
    run(env)
    conn = env[1]
    before = [h.citation.event_id for h in search(conn, "zebra", include_all=True, limit=50)]
    assert len(before) == 3
    conn.execute("INSERT INTO events_fts(events_fts) VALUES ('delete-all')")
    assert search(conn, "zebra", include_all=True) == []
    assert rebuild_fts(conn) == 9
    after = [h.citation.event_id for h in search(conn, "zebra", include_all=True, limit=50)]
    assert sorted(after) == sorted(before)


def test_large_output_blob_written_before_event_and_redacted(env):
    run(env)
    conn = env[1]
    row = conn.execute("SELECT * FROM events WHERE source_id='prt_4'").fetchone()
    assert row["truncated"] == 1 and row["blob_ref"]
    assert (env[3] / row["blob_ref"]).exists()
    assert "deepsecretvalue123" not in (env[3] / row["blob_ref"]).read_text()
    assert row["token_estimate"] == len(row["text"]) // 4
    assert len(row["text"]) < 10_000
    assert (
        conn.execute("SELECT count(*) FROM events WHERE token_estimate IS NULL").fetchone()[0] == 0
    )


def test_disabled_source_is_skipped(env):
    connector, conn, configs, data = env
    run(env)
    conn.execute("UPDATE sources SET enabled = 0 WHERE name='opencode'")
    r = run(env)
    assert r.status == "skipped"


def test_unknown_source_name(env):
    with pytest.raises(ValueError, match="unknown source"):
        run(env, only=["nope"])


def test_snapshot_removed_after_sync(env):
    run(env)
    assert not (env[3] / "tmp" / "opencode-snapshot.db").exists()


def test_dry_run_writes_nothing(env, tmp_path):
    connector, conn, configs, data = env
    results = dry_run({connector.name: connector}, db_file=tmp_path / "absent.db")
    r = results[0]
    assert r.dry_run and r.events_new == 9 and r.sessions_new == 4
    assert not (tmp_path / "absent.db").exists() and not data.exists()


def test_dry_run_counts_existing_as_duplicates(env):
    connector, conn, configs, data = env
    run(env)
    r = dry_run({connector.name: connector}, db_file=tmp_db(conn))[0]
    assert r.events_new == 0 and r.events_duplicate == 9 and r.sessions_new == 0


def tmp_db(conn):
    return conn.execute("PRAGMA database_list").fetchone()["file"]


def test_dry_run_reports_version_error(tmp_path):
    root = tmp_path / "oc"
    root.mkdir()
    sqlite3.connect(root / "opencode.db").close()
    from ctx.connectors.opencode import OpenCodeConnector

    r = dry_run({"opencode": OpenCodeConnector(root)}, db_file=tmp_path / "x.db")[0]
    assert r.status == "error" and "unrecognized" in r.error


# --- redaction before indexing, engine-side (defense in depth) -----------------------

FAKE_SECRETS = {
    "openai": "sk-abcdefghijklmnopqrstuvwx1234",
    "bearer": "Authorization: Bearer abcDEF123456ghiJKL789",
    "github": "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    "envval": "DB_PASSWORD=hunter2hunter2",
    "pem": (
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEAxxxxsecretbody\n"
        "-----END RSA PRIVATE KEY-----"
    ),
}
SECRET_FRAGMENTS = [
    "sk-abcdefghijklmnopqrstuvwx1234",
    "abcDEF123456ghiJKL789",
    "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    "hunter2hunter2",
    "MIIEowIBAAKCAQEAxxxxsecretbody",
]


def test_engine_redacts_unredacted_connector_output_before_index(tmp_path):
    events = [
        raw_event(f"e{i}", f"note {txt} findme") for i, txt in enumerate(FAKE_SECRETS.values())
    ]
    fake = FakeConnector(tmp_path / "fakeroot", events)
    fake.root.mkdir()
    conn = connect(tmp_path / "f.db")
    r = sync(conn, {"fake": fake}, data_dir=tmp_path / "d")[0]
    assert r.events_new == 5
    dump = "\n".join(row[0] for row in conn.execute("SELECT text FROM events"))
    for fragment in SECRET_FRAGMENTS:
        assert fragment not in dump
        assert search(conn, fragment, include_all=True) == []
    assert "[REDACTED" in dump
    assert len(search(conn, "findme", include_all=True, limit=50)) == 5


def test_missing_blob_is_rejected(tmp_path):
    ev = raw_event("e1", "x", truncated=True, blob_ref="blobs/ab/" + "a" * 64)
    fake = FakeConnector(tmp_path / "fr", [ev])
    fake.root.mkdir()
    conn = connect(tmp_path / "m.db")
    r = sync(conn, {"fake": fake}, data_dir=tmp_path / "d")[0]
    assert r.status == "error" and "missing blob" in r.error
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 0  # rolled back


def test_existing_blob_accepted(tmp_path):
    data = tmp_path / "d"
    ref = BlobStore(data).spill("full content")
    fake = FakeConnector(tmp_path / "fr", [raw_event("e1", "x", truncated=True, blob_ref=ref)])
    fake.root.mkdir()
    conn = connect(tmp_path / "m.db")
    assert sync(conn, {"fake": fake}, data_dir=data)[0].blobs == 1


def test_events_are_never_updated_on_resync(tmp_path):
    fake = FakeConnector(tmp_path / "fr", [raw_event("e1", "original text")])
    fake.root.mkdir()
    conn = connect(tmp_path / "u.db")
    sync(conn, {"fake": fake}, data_dir=tmp_path / "d")
    fake.events = [raw_event("e1", "changed upstream", content_hash="1" * 64)]
    r = sync(conn, {"fake": fake}, data_dir=tmp_path / "d")[0]
    assert r.events_new == 0 and r.events_duplicate == 1
    assert conn.execute("SELECT text FROM events").fetchone()[0] == "original text"


def test_batching_commits_large_syncs(tmp_path, monkeypatch):
    import ctx.core.sync as sync_mod

    monkeypatch.setattr(sync_mod, "BATCH_SIZE", 3)
    fake = FakeConnector(tmp_path / "fr", [raw_event(f"e{i}", f"w{i}") for i in range(10)])
    fake.root.mkdir()
    conn = connect(tmp_path / "b.db")
    assert sync(conn, {"fake": fake}, data_dir=tmp_path / "d")[0].events_new == 10
