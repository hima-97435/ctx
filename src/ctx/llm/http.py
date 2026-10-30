"""Minimal JSON-over-HTTP POST with retries, using only the standard library.

Transport is an injectable seam: tests pass a fake, so no test touches the network. Error
messages are built here and never contain request headers (hence never an API key);
response bodies are redacted, key-scrubbed and capped before they appear in a message.
"""

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol

from ctx.core.redact import redact

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_RETRIES = 3  # attempts = 1 + MAX_RETRIES
BACKOFF_BASE_S = 1.0  # 1s, 2s, 4s
BACKOFF_CAP_S = 30.0
BODY_SNIPPET_CHARS = 200


class LLMError(RuntimeError):
    """Any provider failure. Messages never contain API keys."""


class TransportError(OSError):
    """Connection-level failure (DNS, refused, timeout). Retryable."""


@dataclass
class HttpResponse:
    status: int
    body: bytes = b""
    headers: Mapping[str, str] = field(default_factory=dict)


class Transport(Protocol):
    def post(
        self, url: str, headers: Mapping[str, str], body: bytes, timeout: float
    ) -> HttpResponse: ...


class UrllibTransport:
    """The real transport. HTTP error statuses are returned, not raised."""

    def post(
        self, url: str, headers: Mapping[str, str], body: bytes, timeout: float
    ) -> HttpResponse:
        req = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                return HttpResponse(resp.status, resp.read(), dict(resp.headers))
        except urllib.error.HTTPError as exc:
            return HttpResponse(exc.code, exc.read() or b"", dict(exc.headers or {}))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # str(exc) can embed the URL only (never headers); keep just the class name.
            raise TransportError(type(exc).__name__) from None


def scrub(text: str, secrets: tuple[str, ...] = ()) -> str:
    """Redact, remove any literal secret value, collapse whitespace and cap the length."""
    for s in secrets:
        if s:
            text = text.replace(s, "[REDACTED]")
    text = " ".join(redact(text).split())
    return text[:BODY_SNIPPET_CHARS]


def _retry_after(resp: HttpResponse) -> float | None:
    for k, v in resp.headers.items():
        if k.lower() == "retry-after":
            try:
                return max(0.0, float(v))
            except ValueError:
                return None
    return None


def post_json(
    transport: Transport,
    url: str,
    payload: dict,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float = 120.0,
    label: str = "provider",
    secrets: tuple[str, ...] = (),
    sleep: Callable[[float], None] = time.sleep,
    max_retries: int = MAX_RETRIES,
) -> dict:
    """POST JSON, retry 429/5xx and connection errors with exponential backoff (honouring
    Retry-After, capped), return the parsed JSON object. Raises LLMError otherwise."""
    hdrs = {"Content-Type": "application/json", **(headers or {})}
    body = json.dumps(payload).encode("utf-8")
    for attempt in range(max_retries + 1):
        last = attempt == max_retries
        try:
            resp = transport.post(url, hdrs, body, timeout)
        except TransportError as exc:
            if last:
                raise LLMError(
                    f"{label}: cannot reach {url} ({exc}) after {attempt + 1} tries"
                ) from None
            sleep(min(BACKOFF_BASE_S * 2**attempt, BACKOFF_CAP_S))
            continue
        if resp.status in RETRY_STATUSES and not last:
            wait = _retry_after(resp)
            sleep(min(wait if wait is not None else BACKOFF_BASE_S * 2**attempt, BACKOFF_CAP_S))
            continue
        if resp.status >= 400:
            snippet = scrub(resp.body.decode("utf-8", "replace"), secrets)
            raise LLMError(f"{label}: HTTP {resp.status} from {url}: {snippet}")
        try:
            data = json.loads(resp.body)
        except ValueError:
            raise LLMError(f"{label}: response from {url} is not JSON") from None
        if not isinstance(data, dict):
            raise LLMError(f"{label}: unexpected response shape from {url}")
        return data
    raise AssertionError("unreachable")  # pragma: no cover
