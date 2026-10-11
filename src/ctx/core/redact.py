"""Secret redaction.

Applied at ingestion before any text reaches `events`/FTS, and re-applied to any
unredacted (blob) content before display or LLM use (ADR-012, ADR-017). The blob
spilled by the truncate step is a REDACTED copy, not a byte-exact raw record —
callers must redact before spilling.

Placeholders are typed: `[REDACTED:<kind>]`. Patterns are deliberately
conservative toward redaction; redaction is idempotent (placeholders never
re-trigger a pattern). Known limitations are listed in docs/ARCHITECTURE.md.

For files whose *names* mark them sensitive (`.env`, `.env.*`, `credentials`,
`*.pem`, `*.key`), connectors call `redact_file_text(path, text)`, which redacts
every key/value value (key names are not inspected) or the whole file content.
"""

import fnmatch
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# Key/value assignments in JSON, YAML, .env, and shell styles, e.g.
#   api_key = "..."   "password": "..."   SECRET_KEY: ...   MY_TOKEN=...
# A word-shaped prefix is allowed (OPENAI_API_KEY, MY_PASSWORD, ...).
_SECRET_KEYS = (
    r"api[_-]?key|apikey|secret(?:[_-]?key)?|token|access[_-]?token|refresh[_-]?token|"
    r"auth[_-]?token|client[_-]?secret|private[_-]?key|password|passwd|pwd"
)

_KEY_VALUE = re.compile(
    r"(?i)(?P<pre>[\"']?)(?P<key>\b[\w-]{0,32}?(?:" + _SECRET_KEYS + r")\b)(?P<post>[\"']?)"
    r"(?P<sep>\s*[=:]\s*)(?P<q>[\"']?)"
    r"(?P<value>[^\s\"'\[\]]{8,})"
)
_AUTHORIZATION = re.compile(
    r"(?i)\b(?P<hdr>authorization)(?P<sep>\s*:\s*)"
    r"(?:basic\s+|bearer\s+)?(?P<val>[A-Za-z0-9+/=._~\-]{16,})"
)
_BEARER = re.compile(r"(?i)\bbearer(?P<ws>\s+)(?P<val>[A-Za-z0-9._~+/=\-]{16,})")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b")

# One combined pass for all literal-prefix token shapes. Alternative order
# matters: provider-specific prefixes (sk-ant-) come before the generic sk- so
# the per-match classifier can type them correctly.
_TOKEN_SHAPE = re.compile(
    r"\b(?:"
    r"sk-ant-[A-Za-z0-9_\-]{20,}"
    r"|[sr]k_(?:live|test)_[A-Za-z0-9]{16,}"
    r"|sk-[A-Za-z0-9_\-]{20,}"
    r"|nvapi-[A-Za-z0-9_\-]{20,}"
    r"|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|AIza[0-9A-Za-z_\-]{35}"
    r"|xox[baprs]-[A-Za-z0-9\-]{10,}"
    r"|npm_[A-Za-z0-9]{20,}"
    r"|hf_[A-Za-z0-9]{20,}"
    r")\b"
)
_TOKEN_KIND_BY_PREFIX: tuple[tuple[str, str], ...] = (
    ("sk-ant-", "anthropic-key"),
    ("sk_live_", "stripe-key"),
    ("rk_live_", "stripe-key"),
    ("sk_test_", "stripe-key"),
    ("rk_test_", "stripe-key"),
    ("sk-", "api-key"),
    ("nvapi-", "nvapi-key"),
    ("ghp_", "github-token"),
    ("gho_", "github-token"),
    ("ghu_", "github-token"),
    ("ghs_", "github-token"),
    ("ghr_", "github-token"),
    ("AKIA", "aws-access-key"),
    ("AIza", "google-api-key"),
    ("xox", "slack-token"),
    ("npm_", "npm-token"),
    ("hf_", "huggingface-token"),
)


def _token_kind(match: re.Match[str]) -> str:
    token = match.group(0)
    for prefix, kind in _TOKEN_KIND_BY_PREFIX:
        if token.startswith(prefix):
            return kind
    return "api-key"


# The match window is bounded so an unterminated "-----BEGIN" in a huge text
# cannot cause quadratic scanning; real PEM blocks are far below 10K chars.
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY[A-Z0-9 ]*-----"
    r".{0,10000}?"
    r"-----END [A-Z0-9 ]*PRIVATE KEY[A-Z0-9 ]*-----",
    re.DOTALL,
)
_URL_PASSWORD = re.compile(
    r"(?i)\b(?P<scheme>(?>[a-z][a-z0-9+.\-]*))://"  # atomic: no pointless backtracking
    r"(?P<user>[^:/\s@\[\]]+):(?P<pw>[^@\s/\[\]]+)@"
)


@dataclass(frozen=True)
class Redactor:
    name: str
    pattern: re.Pattern[str]
    replacement: str
    # Optional per-match classifier: returns the placeholder kind for a match,
    # used instead of `name` in both the placeholder and the counts.
    kind_of: Callable[[re.Match[str]], str] | None = None


REDACTORS: tuple[Redactor, ...] = (
    Redactor("auth-header", _AUTHORIZATION, r"\g<hdr>\g<sep>[REDACTED:auth-header]"),
    Redactor("bearer", _BEARER, r"Bearer\g<ws>[REDACTED:bearer-token]"),
    Redactor("jwt", _JWT, "[REDACTED:jwt]"),
    Redactor("token-shape", _TOKEN_SHAPE, "[REDACTED]", kind_of=_token_kind),
    Redactor("private-key", _PRIVATE_KEY, "[REDACTED:private-key]"),
    Redactor("secret", _KEY_VALUE, r"\g<pre>\g<key>\g<post>\g<sep>\g<q>[REDACTED:secret]"),
    Redactor("url-password", _URL_PASSWORD, r"\g<scheme>://\g<user>:[REDACTED:url-password]@"),
)

# File names that mark their whole content as sensitive.
SENSITIVE_FILE_PATTERNS: tuple[str, ...] = (".env", ".env.*", "credentials", "*.pem", "*.key")

# .env / credentials style line:  [export] KEY = value   (value = rest of line)
# Spaces/tabs only inside `keep` so `\s` cannot bridge across lines.
_ENV_LINE = re.compile(
    r"(?m)^(?P<keep>[ \t]*(?:export[ \t]+)?[A-Za-z_][A-Za-z0-9_.]*[ \t]*[:=][ \t]*)\S.*$"
)


def redact_counted(text: str) -> tuple[str, dict[str, int]]:
    """Redact and return (text, matches per placeholder kind)."""
    counts: dict[str, int] = {}
    for redactor in REDACTORS:
        if redactor.kind_of is None:
            text, n = redactor.pattern.subn(redactor.replacement, text)
            if n:
                counts[redactor.name] = counts.get(redactor.name, 0) + n
        else:

            def _sub(match: re.Match[str], _redactor: Redactor = redactor) -> str:
                kind = _redactor.kind_of(match)  # type: ignore[misc]
                counts[kind] = counts.get(kind, 0) + 1
                return f"[REDACTED:{kind}]"

            text = redactor.pattern.sub(_sub, text)
    return text, counts


def redact(text: str) -> str:
    """Return `text` with known secret shapes replaced by typed placeholders."""
    return redact_counted(text)[0]


def is_sensitive_file(path: str | Path) -> bool:
    """True when the file's name marks its whole content as sensitive."""
    name = Path(path).name
    return any(fnmatch.fnmatch(name, pattern) for pattern in SENSITIVE_FILE_PATTERNS)


def redact_file_text(path: str | Path, text: str) -> str:
    """Redact ALL values in a sensitive file's content, given its path.

    `.env`/`.env.*`/`credentials`: every key/value line is redacted regardless of
    the key name. `*.pem`/`*.key`: the entire content is replaced. The connector
    calls this with the file path when it detects such a file was read.
    """
    name = Path(path).name
    if fnmatch.fnmatch(name, "*.pem") or fnmatch.fnmatch(name, "*.key"):
        return "[REDACTED:key-file]"
    redacted = _ENV_LINE.sub(r"\g<keep>[REDACTED:env-value]", text)
    return redact(redacted)
