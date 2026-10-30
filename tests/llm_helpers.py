"""Shared fakes for the LLM tests: a scripted HTTP transport and a scripted provider."""

import json

from ctx.llm.http import HttpResponse, TransportError
from ctx.llm.policy import require_clearance
from ctx.llm.providers import Provider

SECRET = "sk-testsecretvalue1234567890abcdef"


class FakeTransport:
    """Replays queued responses (HttpResponse | TransportError) and records every request."""

    def __init__(self, *responses):
        self.queue = list(responses)
        self.requests: list[dict] = []

    def post(self, url, headers, body, timeout):
        self.requests.append(
            {"url": url, "headers": dict(headers), "json": json.loads(body), "timeout": timeout}
        )
        item = self.queue.pop(0) if len(self.queue) > 1 else self.queue[0]
        if isinstance(item, Exception):
            raise item
        return item


def ok(payload: dict) -> HttpResponse:
    return HttpResponse(200, json.dumps(payload).encode())


def chat_ok(text: str) -> HttpResponse:
    return ok({"choices": [{"message": {"content": text}}]})


def status(code: int, body: str = "", **headers) -> HttpResponse:
    return HttpResponse(code, body.encode(), headers)


DOWN = TransportError("URLError")


class ScriptedProvider(Provider):
    """In-process provider: replies from a script, records the messages it was sent."""

    def __init__(self, replies, *, name="fakecloud", is_cloud=True, model="m1"):
        self.name, self.is_cloud, self.model = name, is_cloud, model
        self.replies = [replies] if isinstance(replies, str) else list(replies)
        self.calls: list[list[dict]] = []

    def chat(self, messages, *, clearance, max_tokens=1024, temperature=0.0):
        require_clearance(clearance, self.name)
        self.calls.append(list(messages))
        return self.replies[min(len(self.calls), len(self.replies)) - 1]
