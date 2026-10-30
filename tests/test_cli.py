import json

import pytest
from typer.testing import CliRunner

from ctx.cli.main import app
from tests.helpers import PROJECTS_TOML, make_opencode_root

runner = CliRunner()


@pytest.fixture()
def cli_env(tmp_path, monkeypatch):
    make_opencode_root(tmp_path / "oc")
    monkeypatch.setenv("CTX_SOURCE_ROOT_OPENCODE", str(tmp_path / "oc"))
    for name in ("claude_code", "codex"):  # present but empty: nothing to import
        root = tmp_path / f"empty_{name}"
        (root / ("projects" if name == "claude_code" else "sessions")).mkdir(parents=True)
        monkeypatch.setenv(f"CTX_SOURCE_ROOT_{name.upper()}", str(root))
    home = tmp_path / "ctx_home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "projects.toml").write_text(PROJECTS_TOML)
    return home


# Absent sources (hermes, devin, antigravity_cli here) are skipped by a plain `ctx sync`.
PRESENT: tuple[str, ...] = ()


def invoke(*args):
    return runner.invoke(app, list(args))


def _row(output: str, source: str) -> list[str]:
    """Whitespace-split table row for `source` (status 'not installed' is two words)."""
    for line in output.splitlines():
        if line.startswith(source + " "):
            return line.replace("not installed", "not_installed").split()
    raise AssertionError(f"no row for {source} in:\n{output}")


def test_help_lists_commands():
    out = invoke("--help").output
    for cmd in ("sources", "sync", "search", "context", "reindex"):
        assert cmd in out


def test_sources_list_and_show(cli_env):
    out = invoke("sources", "list").output
    assert "opencode" in out and "yes" in out
    shown = invoke("sources", "show", "opencode").output
    assert "denylist_tables:" in shown and "credential" in shown
    assert "auth.json" in shown and "opencode-structural-v1" in shown
    bad = invoke("sources", "show", "nope")
    assert bad.exit_code == 2 and "unknown source" in bad.output


def test_sync_then_resync_then_sources_list_shows_last_sync(cli_env):
    first = invoke("sync", *PRESENT)
    assert first.exit_code == 0, first.output
    row = _row(first.output, "opencode")
    assert row[1] == "ok" and row[2:4] == ["4", "9"]  # status, sessions+, events+
    second = invoke("sync", "--source", "opencode")
    assert second.exit_code == 0
    row = _row(second.output, "opencode")
    assert row[3] == "0" and row[6] == "1"  # events+ 0, unchanged 1
    third = invoke("sync", "--source", "opencode", "--full")
    assert _row(third.output, "opencode")[3:5] == ["0", "9"]  # events+ 0, dup 9
    listing = invoke("sources", "list").output
    assert "drizzle:" in listing


def test_sync_dry_run_writes_nothing(cli_env):
    r = invoke("sync", "--dry-run", *PRESENT)
    assert r.exit_code == 0 and "[dry-run]" in r.output
    assert _row(r.output, "opencode")[3] == "9"
    assert not (cli_env / "ctx.db").exists()
    assert not (cli_env / "blobs").exists()


def test_sync_unknown_source(cli_env):
    r = invoke("sync", "--source", "nope")
    assert r.exit_code == 2 and "unknown source" in r.output


def test_sync_version_mismatch_exits_nonzero(tmp_path, monkeypatch):
    import sqlite3

    root = tmp_path / "bad"
    root.mkdir()
    sqlite3.connect(root / "opencode.db").close()
    monkeypatch.setenv("CTX_SOURCE_ROOT_OPENCODE", str(root))
    r = invoke("sync")
    assert r.exit_code == 1 and "ERROR" in r.output and "unrecognized" in r.output


def test_search_requires_db(cli_env):
    r = invoke("search", "zebra")
    assert r.exit_code == 1 and "ctx sync" in r.output


def test_search_text_output_and_filters(cli_env):
    invoke("sync")
    r = invoke("search", "zebra")
    assert r.exit_code == 0
    assert "session=ses_2" in r.stdout and "message=msg_ses_2" in r.stdout
    assert "ses_3" not in r.stdout
    assert "ses_3" in invoke("search", "zebra", "--all").stdout
    assert "no results" in invoke("search", "zebra", "--project", "proj").stdout
    assert "ses_2" in invoke("search", "zebra", "--project", "Sub Project").stdout
    assert "no results" in invoke("search", "zebra", "--source", "other").stdout
    assert "no results" in invoke("search", "zebra", "--since", "1d").stdout
    assert (
        "ses_2"
        in invoke("search", "zebra", "--since", "1970-01-01", "--until", "2100-01-01").stdout
    )
    assert "no results" in invoke("search", "zebra", "--until", "1969-12-31").stdout


def test_search_json(cli_env):
    invoke("sync")
    r = invoke("search", "zebra", "--all", "--json", "--limit", "2")
    data = json.loads(r.stdout)
    assert len(data) == 2
    cit = data[0]["citation"]
    assert set(cit) >= {"source", "session_id", "message_id", "ts", "event_id"}
    assert "snippet" in data[0]


def test_search_errors(cli_env):
    invoke("sync")
    assert invoke("search", "zebra", "--project", "nope").exit_code == 2
    assert invoke("search", "zebra", "--since", "garbage").exit_code == 2
    assert invoke("search", "!!!").exit_code == 2


def test_search_never_shows_secrets(cli_env):
    invoke("sync")
    out = invoke("search", "REDACTED", "--all").stdout
    for secret in ("abcdef123456", "deepsecretvalue123", "abcdefgh123"):
        assert secret not in out


def test_context_command(cli_env):
    invoke("sync")
    r = invoke("context", "zebra", "--project", "sub", "--budget", "500")
    assert r.exit_code == 0
    assert "zebra migration plan in ses_2" in r.stdout
    assert "opencode session=ses_2 message=msg_ses_2" in r.stdout
    assert "1 items" in r.stdout and "/500 tokens" in r.stdout
    j = json.loads(invoke("context", "zebra", "--all", "--json").stdout)
    assert len(j["items"]) == 3 and len(j["evidence_event_ids"]) == 3
    assert j["budget_tokens"] == 2000 and j["left_out"] == []
    assert invoke("context", "zebra", "--budget", "0").exit_code == 2  # typer min=1


def test_reindex(cli_env):
    invoke("sync")
    r = invoke("reindex")
    assert r.exit_code == 0 and "reindexed 9 events" in r.output
    assert "ses_2" in invoke("search", "zebra").stdout


def test_entry_point_declared():
    import tomllib
    from pathlib import Path

    data = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    assert data["project"]["scripts"]["ctx"] == "ctx.cli.main:app"


def test_plain_sync_skips_absent_sources_but_reports_them(cli_env):
    r = invoke("sync")
    assert r.exit_code == 0, r.output
    for name in ("hermes", "devin", "antigravity_cli"):
        assert _row(r.output, name)[1] == "not_installed"
        assert f"{name}: not installed" in r.output
    assert _row(r.output, "opencode")[1] == "ok"
    assert _row(r.output, "claude_code")[1] == "ok"  # present but empty


def test_explicit_absent_source_is_an_error(cli_env):
    r = invoke("sync", "--source", "hermes")
    assert r.exit_code == 1
    assert "not installed" in r.output and "hermes" in r.output


def test_explicit_mixed_present_and_absent_still_syncs_present(cli_env):
    r = invoke("sync", "--source", "opencode", "--source", "devin")
    assert r.exit_code == 1
    assert _row(r.output, "opencode")[1] == "ok" and _row(r.output, "devin")[1] == "ERROR"


def test_absent_does_not_mask_failure_of_installed_source(cli_env, tmp_path, monkeypatch):
    import sqlite3

    bad = tmp_path / "bad_oc"
    bad.mkdir()
    sqlite3.connect(bad / "opencode.db").close()
    monkeypatch.setenv("CTX_SOURCE_ROOT_OPENCODE", str(bad))
    r = invoke("sync")
    assert r.exit_code == 1
    assert (
        _row(r.output, "opencode")[1] == "ERROR" and _row(r.output, "hermes")[1] == "not_installed"
    )


def test_dry_run_skips_absent_sources(cli_env):
    r = invoke("sync", "--dry-run")
    assert r.exit_code == 0 and _row(r.output, "hermes")[1] == "not_installed"
    assert invoke("sync", "--dry-run", "--source", "hermes").exit_code == 1


def test_status_before_and_after_sync(cli_env):
    before = json.loads(invoke("status", "--json").stdout)
    assert before["db_exists"] is False and before["projects"] == 0
    assert not (cli_env / "ctx.db").exists()  # status never creates the database
    invoke("sync")
    data = json.loads(invoke("status", "--json").stdout)
    by = {s["name"]: s for s in data["sources"]}
    assert by["opencode"]["installed"] is True and by["opencode"]["events"] == 9
    assert by["opencode"]["sessions"] == 4 and by["opencode"]["last_sync"]
    assert by["hermes"]["installed"] is False and by["hermes"]["events"] == 0
    assert data["projects"] == 3 and data["events_total"] == 9
    assert data["db_path"].endswith("ctx.db")
    text = invoke("status").stdout
    assert "opencode" in text and "hermes" in text and "projects: 3" in text


def test_doctor_healthy_and_json(cli_env):
    invoke("sync")
    r = invoke("doctor")
    assert r.exit_code == 0, r.output
    assert "[ok] fts_integrity" in r.output and "[ok] schema" in r.output
    checks = {c["name"]: c for c in json.loads(invoke("doctor", "--json").stdout)}
    assert {"fts5", "data_dir", "database", "schema", "fts_integrity"} <= set(checks)
    assert all(c["status"] == "ok" for c in checks.values())


def test_doctor_without_database_warns(cli_env):
    r = invoke("doctor")
    assert r.exit_code == 0 and "[warn] database" in r.output and "fix:" in r.output


def test_doctor_detects_fts_drift_and_reindex_fixes_it(cli_env):
    import sqlite3

    invoke("sync")
    db = sqlite3.connect(cli_env / "ctx.db")
    db.execute("INSERT INTO events_fts(events_fts) VALUES ('delete-all')")  # drop the index
    db.commit()
    db.close()
    r = invoke("doctor")
    assert r.exit_code == 1 and "[fail] fts_integrity" in r.output and "ctx reindex" in r.output
    assert invoke("reindex").exit_code == 0
    assert invoke("doctor").exit_code == 0


def test_doctor_reports_pending_and_unknown_migrations(cli_env):
    import sqlite3

    invoke("sync")
    db = sqlite3.connect(cli_env / "ctx.db")
    db.execute("DELETE FROM schema_migrations WHERE name = '0004_sync_state'")
    db.commit()
    r = invoke("doctor")
    assert "[warn] schema" in r.output and "0004_sync_state" in r.output
    db.execute("INSERT INTO schema_migrations VALUES ('9999_future', 'now')")
    db.commit()
    db.close()
    r = invoke("doctor")
    assert r.exit_code == 1 and "[fail] schema" in r.output and "9999_future" in r.output


def test_doctor_unwritable_data_dir(cli_env, monkeypatch, tmp_path):
    import os

    if os.name == "nt" or os.geteuid() == 0:
        pytest.skip("permission bits not enforced")
    ro = tmp_path / "ro_home"
    ro.mkdir()
    ro.chmod(0o500)
    monkeypatch.setenv("CTX_HOME", str(ro))
    try:
        r = invoke("doctor")
    finally:
        ro.chmod(0o700)
    assert r.exit_code == 1 and "[fail] data_dir" in r.output and "CTX_HOME" in r.output
