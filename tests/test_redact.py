import pytest

from ctx.core.redact import redact

# (text, secret substring that must not survive redaction)
MUST_REDACT = [
    ('api_key = "a1b2c3d4e5f6g7h8"', "a1b2c3d4e5f6g7h8"),
    ('"api_key": "a1b2c3d4e5f6g7h8"', "a1b2c3d4e5f6g7h8"),
    ("SECRET_KEY: a1b2c3d4e5f6g7h8", "a1b2c3d4e5f6g7h8"),
    ("password=hunter2hunter2", "hunter2hunter2"),
    ("export DATABASE_PASSWORD='p4ssw0rdp4ssw0rd'", "p4ssw0rdp4ssw0rd"),
    ("client_secret: 0123456789abcdef", "0123456789abcdef"),
    ("OPENAI_API_KEY=abcdef1234567890", "abcdef1234567890"),
    ("token=abcdef123456", "abcdef123456"),
    ("sk-fakefakefakefakefakefake123456", "sk-fakefakefakefake"),
    ("nvapi-fakefakefakefakefakefake123456", "nvapi-fakefakefake"),
    ("ghp_FAKEFAKEFAKEFAKEFAKEFAKE123456", "ghp_FAKEFAKEFAKE"),
    ("AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7"),
    ("AIzaFakeFakeFakeFakeFakeFakeFakeFakeFoo", "AIzaFakeFakeFa"),
    ("xoxb-123456789-abcdefghij", "xoxb-1234567"),
    ("Bearer abcdef1234567890abcd", "abcdef1234567890abcd"),
    ("Authorization: Basic dXNlcm5hbWU6cGFzc3dvcmQxMjM0NQ==", "dXNlcm5hbWU6cGFzc3dvcmQ"),
    (
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
        "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJVadQssw5c",
        "eyJhbGciOiJIUzI1NiJ9",
    ),
    ("postgres://admin:supersecretpw@db.example.com/x", "supersecretpw"),
    (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEpAIBAAKCAQEAfakefakefake\n"
        "-----END RSA PRIVATE KEY-----",
        "MIIEpAIBAAKCAQEAfake",
    ),
]

MUST_NOT_REDACT = [
    "I lost my house key yesterday",
    "token_count = 42",
    "The secret ingredient is patience",
    "password = 1234",
    "https://example.com/path/to/page",
    "redis://localhost:6379/0",
    "OPENAI_API_KEY is set in the environment",
    "See docs for AUTH_TOKEN_NAME before using it",
]


@pytest.mark.parametrize(("text", "secret"), MUST_REDACT)
def test_secret_is_redacted(text, secret):
    out = redact(text)
    assert "[REDACTED" in out
    assert secret not in out, f"secret survived in {out!r}"


@pytest.mark.parametrize("text", MUST_NOT_REDACT)
def test_benign_text_untouched(text):
    assert redact(text) == text


def test_typed_placeholder_for_key_value():
    assert redact('api_key = "a1b2c3d4e5f6g7h8"') == 'api_key = "[REDACTED:secret]"'


def test_prefixed_env_names_are_redacted():
    assert "[REDACTED:secret]" in redact("MY_TOKEN=a1b2c3d4e5f6g7h8")
    assert "[REDACTED:secret]" in redact("STRIPE_SECRET_KEY=sk_dummy_abcdef123456")


def test_multiple_secrets_all_gone():
    text = "\n".join(text for text, _ in MUST_REDACT)
    out = redact(text)
    for _, secret in MUST_REDACT:
        assert secret not in out


def test_redaction_is_idempotent():
    text = "\n".join(text for text, _ in MUST_REDACT)
    once = redact(text)
    assert redact(once) == once
