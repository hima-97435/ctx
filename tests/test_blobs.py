import hashlib

from ctx.core.blobs import BlobStore


def _expected_ref(content: str) -> str:
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return f"blobs/{digest[:2]}/{digest}"


def test_spill_roundtrip(tmp_path):
    store = BlobStore(tmp_path)
    content = "hello blob world\n" * 10
    ref = store.spill(content)
    assert ref == _expected_ref(content)
    assert store.exists(ref)
    assert store.read(ref) == content


def test_spill_is_fsynced_and_file_exists_on_return(tmp_path):
    store = BlobStore(tmp_path)
    ref = store.spill("x" * 100_000)
    path = store.resolve(ref)
    assert path.exists()
    assert path.stat().st_size == 100_000


def test_spill_dedupes_identical_content(tmp_path):
    store = BlobStore(tmp_path)
    ref1 = store.spill("same content")
    ref2 = store.spill("same content")
    assert ref1 == ref2
    files = [p for p in (tmp_path / "blobs").rglob("*") if p.is_file()]
    assert len(files) == 1


def test_different_content_different_ref(tmp_path):
    store = BlobStore(tmp_path)
    ref1 = store.spill("content one")
    ref2 = store.spill("content two")
    assert ref1 != ref2
    assert store.read(ref1) == "content one"
    assert store.read(ref2) == "content two"
