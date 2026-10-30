import pytest
from typer.testing import CliRunner

from ctx.cli.main import app
from tests.test_cli import cli_env  # noqa: F401

runner = CliRunner()


def test_embed_without_extra_gives_clear_message(cli_env, monkeypatch):  # noqa: F811
    assert runner.invoke(app, ["sync"]).exit_code == 0
    monkeypatch.setenv("CTX_EMBED_MODEL", "model2vec-missing:x")  # unknown kind, no download
    res = runner.invoke(app, ["embed"])
    assert res.exit_code == 1 and "unknown embedder" in res.output and "--extra embed" in res.output


def test_search_modes_and_embed_flow(cli_env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("CTX_EMBED_MODEL", "fake:trigram")
    runner.invoke(app, ["sync"])
    plain = runner.invoke(app, ["search", "zebra"])
    assert plain.exit_code == 0 and "ses_2" in plain.output  # auto -> fts, no embeddings
    err = runner.invoke(app, ["search", "zebra", "--mode", "vector"])
    assert err.exit_code == 2 and "ctx embed" in err.output
    first = runner.invoke(app, ["embed"])
    assert first.exit_code == 0 and "fake:trigram" in first.output
    again = runner.invoke(app, ["embed"])
    assert "0 chunks embedded" in again.output
    assert "0 chunks embedded" not in runner.invoke(app, ["embed", "--rebuild"]).output
    for mode in ("auto", "fts", "vector", "hybrid"):
        res = runner.invoke(app, ["search", "zebra migration", "--mode", mode])
        assert res.exit_code == 0 and "ses_2" in res.output, (mode, res.output)
    assert runner.invoke(app, ["search", "zebra", "--mode", "bogus"]).exit_code == 2


@pytest.mark.parametrize("flag,expect", [("1", True), ("", False)])
def test_sync_embeds_only_when_enabled(cli_env, monkeypatch, flag, expect):  # noqa: F811
    monkeypatch.setenv("CTX_EMBED_MODEL", "fake:trigram")
    monkeypatch.setenv("CTX_EMBED_ON_SYNC", flag)
    out = runner.invoke(app, ["sync"]).output
    assert ("embed: " in out) is expect


def test_sync_embed_unavailable_does_not_fail(cli_env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("CTX_EMBED_MODEL", "nomodel:x")
    monkeypatch.setenv("CTX_EMBED_ON_SYNC", "1")
    res = runner.invoke(app, ["sync"])
    assert res.exit_code == 0 and "embed: skipped" in res.output
