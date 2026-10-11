"""Secret redaction.

Applied at ingestion before any text reaches `events`/FTS, and re-applied to any
unredacted (blob) content before display or LLM use (ADR-012). Placeholders are
typed: `[REDACTED:<kind>]`. Patterns are deliberately conservative toward
redaction; known limitations are listed in docs/ARCHITECTURE.md (privacy section).
Redaction is idempotent: placeholders never re-trigger a pattern.
"""

import re
from dataclasses import dataclass

# Key/value assignments in JSON, YAML, .env, and shell styles, e.g.
#   api_key = "..."   "password": "..."   SECRET_KEY: ...   MY_TOKEN=...
# A word-shaped prefix is allowed (OPENAI_API_KEY, MY_PASSWORD, ...).
_SECRET_KEYS = (
    r"api[_-]?key|apikey|secret(?:[_-]?key)?|token|access[_-]?token|refresh[_-]?token|"
    r"auth[_-]?token|client[_-]?secret|private[_-]?key|password|passwd|pwd"
)

_KEY_VALUE = re.compile(
    r"(?i)(?P<pre>[\"']?)(?P<key>\b[\w-]*?(?:" + _SECRET_KEYS + r")\b)(?P<post>[\"']?)"
    r"(?P<sep>\s*[=:]\s*)(?P<q>[\"']?)"
    r"(?P<value>[^\s\"'\[\]]{8,})"
)
_AUTHORIZATION = re.compile(
    r"(?i)\b(?P<hdr>authorization)(?P<sep>\s*:\s*)"
    r"(?:basic\s+|bearer\s+)?(?P<val>[A-Za-z0-9+/=._~\-]{16,})"
)
_BEARER = re.compile(r"(?i)\bbearer(?P<ws>\s+)(?P<val>[A-Za-z0-9._~+/=\-]{16,})")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b")
_OPENAI = re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b")
_NVIDIA = re.compile(r"\bnvapi-[A-Za-z0-9_\-]{20,}\b")
_GITHUB = re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")
_AWS = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
_GOOGLE = re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")
_SLACK = re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b")
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY[A-Z0-9 ]*-----"
    r".*?"
    r"-----END [A-Z0-9 ]*PRIVATE KEY[A-Z0-9 ]*-----",
    re.DOTALL,
)
_URL_PASSWORD = re.compile(
    r"(?i)\b(?P<scheme>[a-z][a-z0-9+.\-]*)://"
    r"(?P<user>[^:/\s@\[\]]+):(?P<pw>[^@\s/\[\]]+)@"
)


@dataclass(frozen=True)
class Redactor:
    name: str
    pattern: re.Pattern[str]
    replacement: str


REDACTORS: tuple[Redactor, ...] = (
    Redactor("auth-header", _AUTHORIZATION, r"\g<hdr>\g<sep>[REDACTED:auth-header]"),
    Redactor("bearer", _BEARER, r"Bearer\g<ws>[REDACTED:bearer-token]"),
    Redactor("jwt", _JWT, "[REDACTED:jwt]"),
    Redactor("api-key", _OPENAI, "[REDACTED:api-key]"),
    Redactor("nvapi-key", _NVIDIA, "[REDACTED:nvapi-key]"),
    Redactor("github-token", _GITHUB, "[REDACTED:github-token]"),
    Redactor("aws-access-key", _AWS, "[REDACTED:aws-access-key]"),
    Redactor("google-api-key", _GOOGLE, "[REDACTED:google-api-key]"),
    Redactor("slack-token", _SLACK, "[REDACTED:slack-token]"),
    Redactor("private-key", _PRIVATE_KEY, "[REDACTED:private-key]"),
    Redactor("secret", _KEY_VALUE, r"\g<pre>\g<key>\g<post>\g<sep>\g<q>[REDACTED:secret]"),
    Redactor("url-password", _URL_PASSWORD, r"\g<scheme>://\g<user>:[REDACTED:url-password]@"),
)


def redact(text: str) -> str:
    """Return `text` with known secret shapes replaced by typed placeholders."""
    for redactor in REDACTORS:
        text = redactor.pattern.sub(redactor.replacement, text)
    return text
