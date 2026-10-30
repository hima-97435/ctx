"""HTTP retries, providers, key handling (no network: fake transports only)."""

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from ctx.llm import policy
from ctx.llm.http import LLMError, UrllibTransport, post_json
from ctx.llm.policy import Clearance, CloudRefused, authorize
from ctx.llm.providers import (
    GeminiProvider,
    LocalProvider,
    MissingKey,
    OpenAICompatibleProvider,
    get_provider,
)
from tests.llm_helpers import DOWN, SECRET, FakeTransport, chat_ok, ok, status


class Sleeps(list):
    def __call__(self, s):
        self.append(s)


def clearance_for(provider):
    class NoDb:
        def execute(self, *a):
            raise AssertionError("local providers never consult the DB")

    return authorize(NoDb(), provider, [])  # local provider: always cleared


def test_retry_429_then_success_honours_retry_after():
    t = FakeTransport(status(429, "slow down", **{"Retry-After": "7"}), status(503), ok({"a": 1}))
    sleeps = Sleeps()
    assert post_json(t, "http://x/y", {}, sleep=sleeps) == {"a": 1}
    assert sleeps == [7.0, 2.0]  # Retry-After, then exponential (attempt 1 -> 2s)
    assert len(t.requests) == 3


def test_gives_up_after_max_retries_without_sleeping_after_last():
    t = FakeTransport(status(500, "boom"))
    sleeps = Sleeps()
    with pytest.raises(LLMError, match="HTTP 500"):
        post_json(t, "http://x/y", {}, sleep=sleeps, max_retries=2)
    assert len(t.requests) == 3
    assert sleeps == [1.0, 2.0]


def test_client_error_not_retried():
    t = FakeTransport(status(400, "bad request"))
    with pytest.raises(LLMError, match="HTTP 400"):
        post_json(t, "http://x/y", {}, sleep=Sleeps())
    assert len(t.requests) == 1


def test_connection_errors_retried_then_reported():
    t = FakeTransport(DOWN, DOWN, ok({"fine": True}))
    assert post_json(t, "http://x/y", {}, sleep=Sleeps()) == {"fine": True}
    t = FakeTransport(DOWN)
    with pytest.raises(LLMError, match="cannot reach"):
        post_json(t, "http://x/y", {}, sleep=Sleeps(), max_retries=1)


def test_non_json_and_non_object_responses():
    from ctx.llm.http import HttpResponse

    for body in (b"<html>", b"[1,2]"):
        with pytest.raises(LLMError):
            post_json(FakeTransport(HttpResponse(200, body)), "http://x", {}, sleep=Sleeps())


def test_error_body_echoing_key_is_scrubbed():
    t = FakeTransport(status(401, f"invalid key {SECRET} for user"))
    with pytest.raises(LLMError) as e:
        post_json(t, "http://x/y", {}, secrets=(SECRET,), sleep=Sleeps())
    assert SECRET not in str(e.value)


def test_urllib_transport_real_loopback_roundtrip():
    seen = {}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            seen["body"] = self.rfile.read(int(self.headers["Content-Length"]))
            seen["auth"] = self.headers.get("Authorization")
            code = 200 if self.path == "/ok" else 429
            self.send_response(code)
            self.end_headers()
            self.wfile.write(b'{"v": 1}')

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{srv.server_port}"
        t = UrllibTransport()
        assert post_json(t, base + "/ok", {"q": 1}, headers={"Authorization": "Bearer z"}) == {
            "v": 1
        }
        assert seen["auth"] == "Bearer z"
        with pytest.raises(LLMError, match="HTTP 429"):
            post_json(t, base + "/limited", {}, sleep=Sleeps(), max_retries=1)
    finally:
        srv.shutdown()
    with pytest.raises(LLMError, match="cannot reach"):
        post_json(t, "http://127.0.0.1:9/x", {}, sleep=Sleeps(), max_retries=0, timeout=1)


# --- providers ---


def test_ollama_request_shape():
    t = FakeTransport(ok({"message": {"content": "hi"}}))
    p = LocalProvider("llama3.2", transport=t)
    assert not p.is_cloud
    assert p.chat([{"role": "user", "content": "q"}], clearance=clearance_for(p)) == "hi"
    req = t.requests[0]
    assert req["url"] == "http://localhost:11434/api/chat"
    assert req["json"]["stream"] is False and req["json"]["model"] == "llama3.2"
    assert "Authorization" not in req["headers"]


def test_openai_compatible_bearer_and_url(monkeypatch):
    monkeypatch.setenv("CTX_NVIDIA_API_KEY", SECRET)
    monkeypatch.setenv("NVIDIA_API_KEY", "other")
    t = FakeTransport(chat_ok("answer"))
    p = get_provider("nvidia", "some/model", transport=t)
    assert p.is_cloud
    cl = Clearance(p.name, frozenset(), _mint=policy._MINT)
    assert p.chat([], clearance=cl) == "answer"
    req = t.requests[0]
    assert req["url"] == "https://integrate.api.nvidia.com/v1/chat/completions"
    assert req["headers"]["Authorization"] == f"Bearer {SECRET}"  # CTX_ var wins
    assert req["json"]["model"] == "some/model"


def test_nvidia_falls_back_to_plain_env_name(monkeypatch):
    monkeypatch.delenv("CTX_NVIDIA_API_KEY", raising=False)
    monkeypatch.setenv("NVIDIA_API_KEY", SECRET)
    from ctx.llm.providers import NVIDIA_KEYS, api_key

    assert api_key(NVIDIA_KEYS, "nvidia") == SECRET


def test_gemini_uses_openai_compatible_endpoint(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", SECRET)
    t = FakeTransport(chat_ok("g"))
    p = GeminiProvider(transport=t)
    assert p.chat([], clearance=Clearance("gemini", frozenset(), _mint=policy._MINT)) == "g"
    assert t.requests[0]["url"] == (
        "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    )
    assert SECRET not in t.requests[0]["url"]  # never in the URL


def test_missing_key_message_names_variables_only(monkeypatch):
    for v in ("CTX_NVIDIA_API_KEY", "NVIDIA_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    t = FakeTransport(chat_ok("x"))
    p = get_provider("nvidia", transport=t)
    with pytest.raises(MissingKey) as e:
        p.chat([], clearance=Clearance("nvidia", frozenset(), _mint=policy._MINT))
    assert "CTX_NVIDIA_API_KEY" in str(e.value) and "NVIDIA_API_KEY" in str(e.value)
    assert t.requests == []  # nothing was sent


def test_key_in_server_error_never_surfaces(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", SECRET)
    t = FakeTransport(status(403, f"key {SECRET} denied"))
    p = GeminiProvider(transport=t)
    with pytest.raises(LLMError) as e:
        p.chat([], clearance=Clearance("gemini", frozenset(), _mint=policy._MINT))
    assert SECRET not in str(e.value) and SECRET not in repr(e.value)


def test_chat_without_valid_clearance_is_refused(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", SECRET)
    t = FakeTransport(chat_ok("x"))
    p = GeminiProvider(transport=t)
    for bad in (None, object(), "ok"):
        with pytest.raises(CloudRefused):
            p.chat([], clearance=bad)
    other = Clearance("nvidia", frozenset(), _mint=policy._MINT)
    with pytest.raises(CloudRefused):  # clearance is per provider
        p.chat([], clearance=other)
    with pytest.raises(TypeError):  # cannot be forged without the mint
        Clearance("gemini", frozenset())
    assert t.requests == []


def test_malformed_chat_response():
    p = OpenAICompatibleProvider("x", "https://h/v1", "m", ["K"], transport=FakeTransport(ok({})))
    with pytest.raises(LLMError):
        p.chat([], clearance=Clearance("x", frozenset(), _mint=policy._MINT))


def test_loopback_openai_compatible_is_not_cloud():
    assert not OpenAICompatibleProvider("lm", "http://127.0.0.1:1234/v1", "m", ["K"]).is_cloud
    assert OpenAICompatibleProvider("o", "https://api.openai.com/v1", "m", ["K"]).is_cloud


def test_unknown_provider():
    with pytest.raises(ValueError, match="unknown provider"):
        get_provider("skynet")
