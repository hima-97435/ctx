import pytest


@pytest.fixture(autouse=True)
def _isolate_ctx_env(tmp_path, monkeypatch):
    """No test may touch the real user data dir or real tool storage."""
    monkeypatch.setenv("CTX_HOME", str(tmp_path / "ctx_home"))
    monkeypatch.delenv("CTX_DATA_DIR", raising=False)
    monkeypatch.delenv("CTX_CONFIG", raising=False)
    monkeypatch.setenv("CTX_SOURCE_ROOT_OPENCODE", str(tmp_path / "no_opencode"))
    monkeypatch.setenv("CTX_SOURCE_ROOT_CLAUDE_CODE", str(tmp_path / "no_claude"))
    monkeypatch.setenv("CTX_SOURCE_ROOT_CODEX", str(tmp_path / "no_codex"))
