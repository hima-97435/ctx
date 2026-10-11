from ctx.core.blobs import BlobStore
from ctx.core.redact import redact
from ctx.core.tokens import estimate_tokens
from ctx.core.truncate import (
    HEAD_CHARS,
    SPILL_THRESHOLD_CHARS,
    TAIL_CHARS,
    prepare_text,
)


def test_small_text_untouched(tmp_path):
    store = BlobStore(tmp_path)
    p = prepare_text("short text", blob_store=store)
    assert p.text == "short text"
    assert p.token_estimate == estimate_tokens("short text")
    assert p.truncated is False
    assert p.blob_ref is None


def test_text_exactly_at_threshold_not_spilled(tmp_path):
    content = "x" * SPILL_THRESHOLD_CHARS
    p = prepare_text(content, blob_store=BlobStore(tmp_path))
    assert p.truncated is False
    assert p.blob_ref is None
    assert p.text == content


def test_oversized_text_spilled_with_head_and_tail(tmp_path):
    store = BlobStore(tmp_path)
    content = "H" * 5_000 + "M" * SPILL_THRESHOLD_CHARS + "T" * 5_000
    p = prepare_text(content, blob_store=store)
    assert p.truncated is True
    assert p.blob_ref is not None
    # full content preserved in the blob
    assert store.read(p.blob_ref) == content
    # kept text = exact head + marker + exact tail
    assert p.text.startswith("H" * HEAD_CHARS)
    assert p.text.endswith("T" * TAIL_CHARS)
    assert f"full content: {p.blob_ref}" in p.text
    assert f"truncated {len(content) - HEAD_CHARS - TAIL_CHARS} chars" in p.text
    assert len(p.text) < len(content)
    assert p.token_estimate == estimate_tokens(p.text)


def test_blob_exists_before_prepare_text_returns(tmp_path):
    """blob-before-event invariant: by the time the caller gets the ref, it is on disk."""
    store = BlobStore(tmp_path)
    p = prepare_text("z" * (SPILL_THRESHOLD_CHARS + 10), blob_store=store)
    assert p.blob_ref is not None
    assert store.exists(p.blob_ref)


def test_prepare_is_deterministic_and_dedupes_blobs(tmp_path):
    store = BlobStore(tmp_path)
    content = "y" * (SPILL_THRESHOLD_CHARS + 123)
    p1 = prepare_text(content, blob_store=store)
    p2 = prepare_text(content, blob_store=store)
    assert p1.blob_ref == p2.blob_ref
    files = [x for x in (tmp_path / "blobs").rglob("*") if x.is_file()]
    assert len(files) == 1


def test_redact_before_spill_so_blobs_hold_no_secrets(tmp_path):
    """Documented pipeline: redact -> truncate/spill (ADR-012 + ADR-004)."""
    store = BlobStore(tmp_path)
    raw = "token=supersecretvalue123\n" * 5_000  # ~125K chars, oversized
    p = prepare_text(redact(raw), blob_store=store)
    assert p.truncated is True
    assert "supersecretvalue123" not in p.text
    assert "supersecretvalue123" not in store.read(p.blob_ref)
    assert "[REDACTED:secret]" in p.text
