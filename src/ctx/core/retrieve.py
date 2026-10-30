"""The ONE retrieval path: FTS5, vector, or hybrid (Reciprocal Rank Fusion).

`ctx search`, `build_context` (hence `ctx context`, MCP `get_context`), MCP `search` and
`ctx eval` all call `retrieve`; nothing else ranks events (AGENTS rule 8). Filters and
visibility come from `search.build_filters`, so every mode applies them identically.

`SearchHit.rank` is "lower is better" in every mode (bm25; -cosine; -RRF score) and only
ordering is meaningful across modes.
"""

import re
import sqlite3
from dataclasses import replace

from ctx.core.embed import (
    Embedder,
    EmbedderUnavailable,
    _numpy,
    check_embedder_allowed,
    chunk_text,
    default_spec,
    embed_normalized,
    get_embedder,
)
from ctx.core.redact import redact
from ctx.core.search import SearchHit, build_filters, hits_for_events, resolve_project, search

MODES = ("fts", "vector", "hybrid")
RRF_K = 60  # standard RRF constant (Cormack et al. 2009)
POOL_MIN = 50  # candidates taken from each ranker before fusion
SNIPPET_CHARS = 240


class NoEmbeddings(RuntimeError):
    """vector/hybrid was requested but no embeddings exist for the model."""


def resolve_mode(
    conn: sqlite3.Connection, mode: str = "auto", embedder: Embedder | None = None
) -> tuple[str, Embedder | None, str | None]:
    """Decide how to retrieve. Returns (mode, embedder, note).

    `auto` = hybrid when embeddings exist for the default model and its embedder loads,
    else fts (with a note when embeddings exist but the embedder cannot load). Explicit
    `vector`/`hybrid` raise EmbedderUnavailable / NoEmbeddings with an actionable message.
    """
    if mode not in (*MODES, "auto"):
        raise ValueError(f"unknown mode {mode!r}; use auto, fts, vector or hybrid")
    if mode == "fts":
        return "fts", None, None
    if mode == "auto":
        spec = embedder.name if embedder is not None else default_spec()
        has = conn.execute("SELECT 1 FROM embeddings WHERE model = ? LIMIT 1", (spec,)).fetchone()
        if not has:
            return "fts", None, None
        try:
            return "hybrid", embedder or get_embedder(spec), None
        except EmbedderUnavailable as exc:
            return "fts", None, f"using fts only: {exc}"
    try:
        emb = embedder or get_embedder()
    except EmbedderUnavailable as exc:
        raise EmbedderUnavailable(f"{mode} search unavailable: {exc}") from exc
    has = conn.execute("SELECT 1 FROM embeddings WHERE model = ? LIMIT 1", (emb.name,)).fetchone()
    if not has:
        raise NoEmbeddings(f"no embeddings for {emb.name!r}; run `ctx embed` first")
    return mode, emb, None


def _vector_ranked(
    conn: sqlite3.Connection,
    query: str,
    embedder: Embedder,
    where: list[str],
    params: list[object],
    pool: int,
) -> list[tuple[int, int, float]]:
    """(event_id, best_chunk_idx, cosine) best-first, one entry per event, brute force."""
    np = _numpy()
    q = embed_normalized(embedder, [query])[0]
    sql = (
        "SELECT em.event_id, em.chunk_idx, em.vector FROM embeddings em"
        " JOIN events e ON e.id = em.event_id LEFT JOIN sessions s ON s.id = e.session_id"
        f" WHERE em.model = ? AND em.dim = ?{''.join(' AND ' + w for w in where)}"
        " ORDER BY em.event_id, em.chunk_idx"
    )
    rows = conn.execute(sql, [embedder.name, embedder.dim, *params]).fetchall()
    if not rows:
        return []
    mat = np.frombuffer(b"".join(r["vector"] for r in rows), dtype=np.float32).reshape(
        len(rows), embedder.dim
    )
    scores = mat @ q
    best: dict[int, tuple[float, int]] = {}
    for r, sc in zip(rows, scores.tolist(), strict=True):
        cur = best.get(r["event_id"])
        if cur is None or sc > cur[0]:
            best[r["event_id"]] = (sc, r["chunk_idx"])
    ranked = sorted(best.items(), key=lambda kv: (-kv[1][0], kv[0]))[:pool]
    return [(eid, idx, sc) for eid, (sc, idx) in ranked]


def _chunk_snippet(conn: sqlite3.Connection, event_id: int, chunk_idx: int) -> str:
    row = conn.execute("SELECT text FROM events WHERE id = ?", (event_id,)).fetchone()
    chunks = chunk_text(redact(row["text"])) if row else []
    text = chunks[chunk_idx] if chunk_idx < len(chunks) else ""
    return re.sub(r"\s+", " ", text)[:SNIPPET_CHARS]


def retrieve(
    conn: sqlite3.Connection,
    query: str,
    *,
    mode: str = "auto",
    embedder: Embedder | None = None,
    project: str | None = None,
    source: str | None = None,
    since: int | None = None,
    until: int | None = None,
    include_all: bool = False,
    exclude_mcp: bool = False,
    limit: int = 20,
    include_text: bool = False,
    rrf_k: int = RRF_K,
) -> list[SearchHit]:
    """Ranked hits with citations in `fts`, `vector` or `hybrid` mode (see module doc)."""
    mode, embedder, _note = resolve_mode(conn, mode, embedder)
    fts_args = {
        "project": project,
        "source": source,
        "since": since,
        "until": until,
        "include_all": include_all,
        "exclude_mcp": exclude_mcp,
    }
    if mode == "fts":
        return search(conn, query, **fts_args, limit=limit, include_text=include_text)
    assert embedder is not None
    project_id = resolve_project(conn, project) if project is not None else None
    check_embedder_allowed(conn, embedder, project_id)  # rule 9, before the query leaves
    where, params = build_filters(conn, **fts_args)
    pool = max(limit * 4, POOL_MIN)
    vec = _vector_ranked(conn, query, embedder, where, params, pool)
    if mode == "vector":
        scored = [(eid, _chunk_snippet(conn, eid, idx), -sc) for eid, idx, sc in vec[:limit]]
        return hits_for_events(conn, scored, include_text=include_text)
    try:
        fts_hits = search(conn, query, **fts_args, limit=pool, include_text=include_text)
    except ValueError:  # query without any word characters: vector side still works
        fts_hits = []
    fused: dict[int, float] = {}
    for rank, h in enumerate(fts_hits, 1):
        fused[h.citation.event_id] = fused.get(h.citation.event_id, 0.0) + 1.0 / (rrf_k + rank)
    for rank, (eid, _idx, _sc) in enumerate(vec, 1):
        fused[eid] = fused.get(eid, 0.0) + 1.0 / (rrf_k + rank)
    order = sorted(fused.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    by_fts = {h.citation.event_id: h for h in fts_hits}
    vec_idx = {eid: idx for eid, idx, _ in vec}
    out: list[SearchHit | None] = [None] * len(order)
    missing = []
    for n, (eid, score) in enumerate(order):
        if eid in by_fts:  # keep the FTS snippet (it has the match highlighted)
            out[n] = replace(by_fts[eid], rank=-score)
        else:
            missing.append((n, (eid, _chunk_snippet(conn, eid, vec_idx[eid]), -score)))
    fetched = hits_for_events(conn, [m for _, m in missing], include_text=include_text)
    for (n, _), hit in zip(missing, fetched, strict=True):
        out[n] = hit
    return [h for h in out if h is not None]
