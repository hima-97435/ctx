"""Embeddings: deterministic chunking, the Embedder interface, and the derived `embeddings` table.

Everything here is derived from `events` alone and rebuildable (`ctx embed --rebuild`); it
never writes to `events` (AGENTS rule 1). Text is redacted *before* it is chunked or
embedded (rule 4), so a vector never encodes a secret that redaction would have removed.

Embedders are pluggable. A local one ships (model2vec, optional extra `embed`). The
interface carries `is_cloud` so the local-only rule (rule 9) is enforced in code: a cloud
embedder may never see text from, or a query scoped to, a `local_only=1` project (or an
unlinked event, unless the `unlinked` pseudo-project opted in). Cloud embedders (Gemini,
NVIDIA) live in `ctx.llm.embedders` and share the chat policy predicate (ADR-029).
"""

import hashlib
import os
import re
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from ctx.core.redact import redact
from ctx.llm.policy import UNLINKED_PROJECT_ID, cloud_scope_refusal

CHUNK_CHARS = 1000  # ~250 tokens: small enough for static/small models, big enough for context
MAX_CHUNKS_PER_EVENT = 16  # head only; FTS still covers the rest of very large tool outputs
DEFAULT_MODEL = "model2vec:minishlab/potion-base-8M"
EMBED_EXTRA_HINT = (
    "install the optional extra: uv sync --extra embed (or: pip install 'ctx[embed]')"
)
_BATCH_EVENTS = 128


class EmbedderUnavailable(RuntimeError):
    """The embedder (or its optional dependency / cached model) is not available."""


class LocalOnlyViolation(ValueError):
    """A cloud embedder was asked to touch local_only data (rule 9)."""


class Embedder(Protocol):
    name: str  # stored in embeddings.model; changing it means a different vector space
    dim: int
    is_cloud: bool

    def embed(self, texts: Sequence[str]) -> "np.ndarray":  # noqa: F821
        """Return a float32 array of shape (len(texts), dim)."""
        ...


def chunk_text(text: str, max_chars: int = CHUNK_CHARS) -> list[str]:
    """Deterministic chunking: pack paragraphs, then lines, then fixed-width slices.

    Pure function of the text: the same text always yields the same chunks, so
    `text_hash` makes re-embedding incremental.
    """
    text = text.strip()
    if not text:
        return []
    chunks = _pack(re.split(r"\n{2,}", text), "\n\n", max_chars, _split_lines)
    return chunks[:MAX_CHUNKS_PER_EVENT]


def _split_lines(block: str, max_chars: int) -> list[str]:
    return _pack(block.split("\n"), "\n", max_chars, _split_fixed)


def _split_fixed(line: str, max_chars: int) -> list[str]:
    return [line[i : i + max_chars] for i in range(0, len(line), max_chars)]


def _pack(units: list[str], sep: str, max_chars: int, splitter: Callable) -> list[str]:
    out: list[str] = []
    cur = ""
    for unit in units:
        unit = unit.strip()
        if not unit:
            continue
        pieces = [unit] if len(unit) <= max_chars else splitter(unit, max_chars)
        for piece in pieces:
            if cur and len(cur) + len(sep) + len(piece) > max_chars:
                out.append(cur)
                cur = piece
            else:
                cur = f"{cur}{sep}{piece}" if cur else piece
    if cur:
        out.append(cur)
    return out


def text_hash(chunk: str) -> str:
    return hashlib.sha256(chunk.encode("utf-8")).hexdigest()


# --- registry --------------------------------------------------------------------------

_FACTORIES: dict[str, Callable[[str], Embedder]] = {}


def register_embedder(kind: str, factory: Callable[[str], Embedder]) -> None:
    """Register `factory(arg)` for specs of the form `<kind>:<arg>` (tests add a fake)."""
    _FACTORIES[kind] = factory


def default_spec() -> str:
    return os.environ.get("CTX_EMBED_MODEL") or DEFAULT_MODEL


def get_embedder(spec: str | None = None) -> Embedder:
    spec = spec or default_spec()
    import ctx.llm.embedders  # noqa: F401  (registers the cloud kinds; imported lazily: cycle)

    kind, _, arg = spec.partition(":")
    factory = _FACTORIES.get(kind)
    if factory is None:
        raise EmbedderUnavailable(
            f"unknown embedder {spec!r}; known kinds: {', '.join(sorted(_FACTORIES))}"
        )
    return factory(arg)


class Model2VecEmbedder:
    """Static (no torch) local embedder. The first use downloads the model files from the
    Hugging Face hub into its normal cache; no ctx data is ever sent."""

    is_cloud = False

    def __init__(self, model_id: str) -> None:
        try:
            from model2vec import StaticModel
        except ImportError as exc:
            raise EmbedderUnavailable(f"model2vec is not installed; {EMBED_EXTRA_HINT}") from exc
        try:
            self._model = StaticModel.from_pretrained(model_id)
        except Exception as exc:  # network / missing model: report, don't crash a sync
            raise EmbedderUnavailable(f"cannot load embedding model {model_id!r}: {exc}") from exc
        self.name = f"model2vec:{model_id}"
        self.dim = int(self._model.dim)

    def embed(self, texts: Sequence[str]):
        import numpy as np

        return np.asarray(self._model.encode(list(texts)), dtype=np.float32)


register_embedder("model2vec", Model2VecEmbedder)


def _numpy():
    try:
        import numpy as np
    except ImportError as exc:
        raise EmbedderUnavailable(f"numpy is not installed; {EMBED_EXTRA_HINT}") from exc
    return np


def embed_normalized(embedder: Embedder, texts: Sequence[str]):
    """Embed and L2-normalize, so cosine similarity is a plain dot product."""
    np = _numpy()
    vecs = np.asarray(embedder.embed(list(texts)), dtype=np.float32).reshape(len(texts), -1)
    if vecs.shape[1] != embedder.dim:
        raise ValueError(f"embedder {embedder.name} returned dim {vecs.shape[1]} != {embedder.dim}")
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (vecs / norms).astype(np.float32)


# --- local-only enforcement ------------------------------------------------------------


def check_embedder_allowed(
    conn: sqlite3.Connection, embedder: Embedder, project_id: str | None
) -> None:
    """A cloud embedder may only handle a query scoped to one `local_only=0` project."""
    if not embedder.is_cloud:
        return
    if project_id is None:
        raise LocalOnlyViolation(
            f"cloud embedder {embedder.name!r} needs a project scope with local_only=0"
        )
    why = cloud_scope_refusal(conn, [project_id])
    if why is not None:
        raise LocalOnlyViolation(f"cloud embedder {embedder.name!r} refused: {why}")


# --- building the derived table --------------------------------------------------------


@dataclass
class EmbedStats:
    model: str
    events_scanned: int = 0
    chunks_embedded: int = 0
    chunks_reused: int = 0  # vector copied from an identical text_hash
    chunks_unchanged: int = 0
    events_skipped_local_only: int = 0
    total_chunks: int = 0


def embed_events(
    conn: sqlite3.Connection, embedder: Embedder, *, rebuild: bool = False
) -> EmbedStats:
    """Create/refresh embeddings for every event, incrementally by `text_hash`.

    Chunks whose (event, index, hash) already exist are untouched; identical text seen
    elsewhere reuses its vector; only genuinely new text reaches the embedder. A cloud
    embedder skips events of `local_only=1` projects and unlinked events.
    """
    stats = EmbedStats(model=embedder.name)
    conn.execute("BEGIN")
    try:
        if rebuild:
            conn.execute("DELETE FROM embeddings WHERE model = ?", (embedder.name,))
        else:  # same model name but another dimension means a different vector space
            conn.execute(
                "DELETE FROM embeddings WHERE model = ? AND dim != ?", (embedder.name, embedder.dim)
            )
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    cloud_filter = (
        " AND (COALESCE(s.project_id, e.project_id) IN"
        " (SELECT id FROM projects WHERE local_only=0 AND id != ?)"
        " OR (COALESCE(s.project_id, e.project_id) IS NULL AND EXISTS"
        " (SELECT 1 FROM projects WHERE id = ? AND local_only=0)))"
        if embedder.is_cloud
        else ""
    )
    cloud_args = (UNLINKED_PROJECT_ID, UNLINKED_PROJECT_ID) if embedder.is_cloud else ()
    last = 0
    while True:
        rows = conn.execute(
            "SELECT e.id, e.text FROM events e LEFT JOIN sessions s ON s.id = e.session_id"
            f" WHERE e.id > ?{cloud_filter} ORDER BY e.id LIMIT ?",
            (last, *cloud_args, _BATCH_EVENTS),
        ).fetchall()
        if not rows:
            break
        last = rows[-1]["id"]
        _embed_batch(conn, embedder, rows, stats)
    if embedder.is_cloud:
        stats.events_skipped_local_only = (
            conn.execute("SELECT count(*) FROM events").fetchone()[0] - stats.events_scanned
        )
    stats.total_chunks = conn.execute(
        "SELECT count(*) FROM embeddings WHERE model = ?", (embedder.name,)
    ).fetchone()[0]
    return stats


def _embed_batch(
    conn: sqlite3.Connection, embedder: Embedder, rows: list[sqlite3.Row], stats: EmbedStats
) -> None:
    np = _numpy()
    ids = [r["id"] for r in rows]
    marks = ",".join("?" * len(ids))
    existing: dict[tuple[int, int], str] = {
        (r["event_id"], r["chunk_idx"]): r["text_hash"]
        for r in conn.execute(
            f"SELECT event_id, chunk_idx, text_hash FROM embeddings"
            f" WHERE model = ? AND event_id IN ({marks})",
            [embedder.name, *ids],
        )
    }
    wanted: dict[tuple[int, int], str] = {}  # (event, idx) -> chunk text
    for r in rows:
        stats.events_scanned += 1
        for idx, chunk in enumerate(chunk_text(redact(r["text"]))):  # redact BEFORE embedding
            wanted[(r["id"], idx)] = chunk
    todo = {k: c for k, c in wanted.items() if existing.get(k) != text_hash(c)}
    stats.chunks_unchanged += len(wanted) - len(todo)
    vectors: dict[str, bytes] = {}
    for h in {text_hash(c) for c in todo.values()}:
        row = conn.execute(
            "SELECT vector FROM embeddings WHERE model = ? AND text_hash = ? LIMIT 1",
            (embedder.name, h),
        ).fetchone()
        if row is not None:
            vectors[h] = row["vector"]
    fresh = {}
    for c in todo.values():
        h = text_hash(c)
        if h not in vectors and h not in fresh:
            fresh[h] = c
    if fresh:
        mat = embed_normalized(embedder, list(fresh.values()))
        for h, vec in zip(fresh, mat, strict=True):
            vectors[h] = np.ascontiguousarray(vec, dtype=np.float32).tobytes()
    stats.chunks_embedded += len(fresh)
    stats.chunks_reused += len(todo) - len(fresh)
    conn.execute("BEGIN")
    try:
        for (eid, idx), chunk in todo.items():
            h = text_hash(chunk)
            conn.execute(
                "INSERT OR REPLACE INTO embeddings(event_id, chunk_idx, model, dim, vector,"
                " text_hash) VALUES (?, ?, ?, ?, ?, ?)",
                (eid, idx, embedder.name, embedder.dim, vectors[h], h),
            )
        # chunks that no longer exist (chunker change) are dropped
        counts: dict[int, int] = {}
        for eid, _idx in wanted:
            counts[eid] = counts.get(eid, 0) + 1
        for eid in ids:
            n = counts.get(eid, 0)
            conn.execute(
                "DELETE FROM embeddings WHERE model = ? AND event_id = ? AND chunk_idx >= ?",
                (embedder.name, eid, n),
            )
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def embed_on_sync_enabled() -> bool:
    return os.environ.get("CTX_EMBED_ON_SYNC", "").strip().lower() in {"1", "true", "yes", "on"}


def embedding_count(conn: sqlite3.Connection, model: str | None = None) -> int:
    if model is None:
        return conn.execute("SELECT count(*) FROM embeddings").fetchone()[0]
    return conn.execute("SELECT count(*) FROM embeddings WHERE model = ?", (model,)).fetchone()[0]
