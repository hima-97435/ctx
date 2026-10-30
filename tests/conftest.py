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
    monkeypatch.setenv("CTX_SOURCE_ROOT_HERMES", str(tmp_path / "no_hermes"))
    monkeypatch.setenv("CTX_SOURCE_ROOT_DEVIN", str(tmp_path / "no_devin"))
    monkeypatch.setenv("CTX_SOURCE_ROOT_ANTIGRAVITY_CLI", str(tmp_path / "no_agy"))
    for name in ("CHATGPT", "CLAUDE_AI", "GEMINI"):
        monkeypatch.setenv(f"CTX_SOURCE_ROOT_{name}", str(tmp_path / f"no_{name.lower()}"))
    for var in ("HERMES_HOME", "CHISEL_SESSION_DB", "AGY_CONVERSATIONS_DIR"):
        monkeypatch.delenv(var, raising=False)


# Offline embedders for specs like CTX_EMBED_MODEL=fake:trigram (never a real model).
from ctx.core.embed import register_embedder  # noqa: E402
from tests.fake_embedder import FakeEmbedder  # noqa: E402

register_embedder("fake", lambda arg: FakeEmbedder(name=f"fake:{arg}" if arg else "fake:trigram"))
