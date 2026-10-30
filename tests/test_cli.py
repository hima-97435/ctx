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


# Sources whose storage the cli_env fixture provides (the other connectors report an
# error for storage that is not installed, which is correct but not what these tests check).
PRESENT = ("--source", "opencode", "--source", "claude_code", "--source", "codex")


def invoke(*args):
    return runner.invoke(app, list(args))


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
    assert "events 9 (added 9, 0 already present)" in first.output
    second = invoke("sync", "--source", "opencode")
    assert second.exit_code == 0
    assert "added 0, 9 already present" in second.output
    listing = invoke("sources", "list").output
    assert "drizzle:" in listing


def test_sync_dry_run_writes_nothing(cli_env):
    r = invoke("sync", "--dry-run", *PRESENT)
    assert r.exit_code == 0 and "[dry-run]" in r.output and "would add 9" in r.output
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
