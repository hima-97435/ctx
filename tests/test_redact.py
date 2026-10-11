import time

import pytest

from ctx.core.redact import is_sensitive_file, redact, redact_file_text

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
    ("sk-ant-api03-fakefakefakefakefake123456", "sk-ant-api03-fakefake"),
    ("sk_live_FAKEFAKEFAKEFAKEFAKE", "sk_live_FAKEFAKE"),
    ("rk_live_FAKEFAKEFAKEFAKEFAKE", "rk_live_FAKEFAKE"),
    ("npm_FAKEFAKEFAKEFAKEFAKEFAKE12", "npm_FAKEFAKEFAKE"),
    ("hf_FAKEFAKEFAKEFAKEFAKEFAKE12", "hf_FAKEFAKEFAKE"),
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


def test_anthropic_typed_before_generic_sk():
    out = redact("key: sk-ant-api03-fakefakefakefakefake123456")
    assert "[REDACTED:anthropic-key]" in out
    assert "[REDACTED:api-key]" not in out


def test_is_sensitive_file():
    assert is_sensitive_file("/a/b/.env")
    assert is_sensitive_file("/a/b/.env.local")
    assert is_sensitive_file("credentials")
    assert is_sensitive_file("/x/server.pem")
    assert is_sensitive_file("id_ed25519.key")
    assert not is_sensitive_file("/x/notes.txt")
    assert not is_sensitive_file("/x/credentials.json")


def test_env_file_redacts_all_values():
    text = (
        "DATABASE_URL=postgres://u:p@h/db\n"
        "DEBUG=true\n"
        "# a comment stays\n"
        "EMPTY=\n"
        'export API_THING="abcdefgh123"\n'
    )
    out = redact_file_text("/proj/.env", text)
    assert "postgres://u:p@h/db" not in out
    assert "abcdefgh123" not in out
    assert "DEBUG=[REDACTED:env-value]" in out
    assert "EMPTY=" in out
    assert "# a comment stays" in out
    assert "export API_THING=[REDACTED:env-value]" in out


def test_env_dot_local_is_sensitive():
    out = redact_file_text("/proj/.env.local", "MY_WEIRD_KEY=somevalue12345")
    assert "somevalue12345" not in out
    assert "[REDACTED:env-value]" in out


def test_credentials_ini_redacted():
    text = (
        "[default]\n"
        "aws_access_key_id = AKIAIOSFODNN7EXAMPLE\n"
        "aws_secret_access_key = wJalrXUtnFEMI\n"
    )
    out = redact_file_text("credentials", text)
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert "wJalrXUtnFEMI" not in out
    assert "[REDACTED:env-value]" in out
    assert "[default]" in out


def test_key_file_content_replaced_wholesale():
    pem = "-----BEGIN CERTIFICATE-----\nabc\n-----END CERTIFICATE-----\ntrailing"
    assert redact_file_text("/etc/ssl/server.pem", pem) == "[REDACTED:key-file]"
    assert redact_file_text("id_rsa.key", "random secret material") == "[REDACTED:key-file]"


def _adversarial_5mb() -> str:
    parts = [
        ("sk-" + "a" * 19 + " ") * 60_000,  # near-miss keys, boundary failures
        ("token" + "b" * 400 + " ") * 1_500,  # lazy-prefix stress
        ("-----BEGIN PRIVATE KEY-----\n" + "c" * 100 + "\n") * 3_000,  # unterminated PEM
        ("postgres://user:" + "d" * 150 + "/x ") * 2_000,  # URL near-miss
        "AKIAIOSFODNN7EXAMPLE deep in the middle ",
    ]
    text = "".join(parts)
    pad = 5 * 1024 * 1024 - len(text)
    if pad > 0:
        text += "Z" * pad
    assert len(text) >= 5 * 1024 * 1024
    return text


def test_adversarial_5mb_redacts_quickly():
    text = _adversarial_5mb()
    start = time.perf_counter()
    out = redact(text)
    elapsed = time.perf_counter() - start
    assert elapsed < 2.0, f"redaction of 5MB adversarial input took {elapsed:.2f}s"
    assert "AKIAIOSFODNN7EXAMPLE" not in out
