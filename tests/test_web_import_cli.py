import json

from typer.testing import CliRunner

from ctx.cli.main import app
from tests.fixtures.web_exports import chatgpt_zip, claude_zip, gemini_zip, make_zip

runner = CliRunner()


def invoke(*args):
    return runner.invoke(app, list(args))


def test_import_each_kind_and_reimport(tmp_path):
    cases = {
        "chatgpt": (chatgpt_zip(tmp_path / "ext" / "gpt.zip"), "alembic"),
        "claude-ai": (claude_zip(tmp_path / "ext" / "cl.zip"), "summary"),
        "gemini": (gemini_zip(tmp_path / "ext" / "gem.zip"), "Answer"),
    }
    for kind, (path, word) in cases.items():
        first = invoke("import", kind, str(path))
        assert first.exit_code == 0, first.output
        second = invoke("import", kind, str(path))
        assert second.exit_code == 0 and " 0 " in second.output  # events+ 0
        hit = invoke("search", word, "--all", "--json")
        assert json.loads(hit.output), kind
    assert invoke("sources", "show", "chatgpt").exit_code == 0


def test_import_errors(tmp_path):
    assert invoke("import", "chatgpt", str(tmp_path / "nope.zip")).exit_code == 2
    z = chatgpt_zip(tmp_path / "x.zip")
    bad = invoke("import", "bard", str(z))
    assert bad.exit_code == 2 and "unknown export kind" in bad.output
    wrong = invoke("import", "claude-ai", str(z))  # a ChatGPT zip is not a Claude export
    assert wrong.exit_code == 1 and "ERROR" in wrong.output
    broken = make_zip(tmp_path / "b.zip", {"conversations.json": "{"})
    assert invoke("import", "chatgpt", str(broken)).exit_code == 1


def test_import_dry_run_writes_nothing(tmp_path):
    z = chatgpt_zip(tmp_path / "x.zip")
    r = invoke("import", "chatgpt", str(z), "--dry-run")
    assert r.exit_code == 0 and "dry-run" in r.output
    assert invoke("search", "alembic", "--all").exit_code == 1  # no database created


def test_plain_sync_picks_up_watch_dir_and_absent_is_not_error(tmp_path, monkeypatch):
    home = tmp_path / "ctx_home"
    monkeypatch.delenv("CTX_SOURCE_ROOT_CHATGPT")
    monkeypatch.delenv("CTX_SOURCE_ROOT_CLAUDE_AI")
    monkeypatch.delenv("CTX_SOURCE_ROOT_GEMINI")
    first = invoke("sync")
    assert first.exit_code == 0, first.output
    assert "not installed" in first.output and "chatgpt" in first.output
    chatgpt_zip(home / "imports" / "chatgpt" / "export.zip")
    second = invoke("sync")
    assert second.exit_code == 0, second.output
    row = next(ln for ln in second.output.splitlines() if ln.startswith("chatgpt "))
    assert row.split()[1] == "ok" and row.split()[3] == "8"
    third = invoke("sync")
    row = next(ln for ln in third.output.splitlines() if ln.startswith("chatgpt "))
    assert row.split()[3] == "0"
    explicit = invoke("sync", "--source", "gemini")  # named but absent: error
    assert explicit.exit_code == 1
