"""Embeddings table, vector/hybrid retrieval, local-only enforcement, one-path wiring."""

import pytest

from ctx.core.context import build_context
from ctx.core.embed import (
    EmbedderUnavailable,
    LocalOnlyViolation,
    chunk_text,
    embed_events,
    get_embedder,
    text_hash,
)
from ctx.core.retrieve import NoEmbeddings, resolve_mode, retrieve
from ctx.core.search import search
from tests.fake_embedder import FakeCloudEmbedder, FakeEmbedder
from tests.fixtures.eval_corpus import build_eval_db
from tests.test_mcp import call, text_of


@pytest.fixture()
def conn(tmp_path):
    return build_eval_db(tmp_path)


@pytest.fixture()
def emb():
    return FakeEmbedder()


@pytest.fixture()
def embedded(conn, emb):
    embed_events(conn, emb)
    return conn


def sessions(hits):
    return [h.citation.source_session_id for h in hits]


# --- chunking ---------------------------------------------------------------------------


def test_chunking_is_deterministic_and_bounded():
    text = "para one\n\n" + ("word " * 400) + "\n\n" + "x" * 2500 + "\nline"
    a, b = chunk_text(text, 500), chunk_text(text, 500)
    assert a == b and all(0 < len(c) <= 500 for c in a)
    assert chunk_text("   \n\n ") == []
    assert chunk_text("short") == ["short"]
    assert len(chunk_text("y\n\n" * 5000, 10)) == 16  # MAX_CHUNKS_PER_EVENT


# --- derived table ----------------------------------------------------------------------


def test_embed_is_incremental_and_rebuildable(conn, emb):
    s1 = embed_events(conn, emb)
    assert s1.chunks_embedded == s1.total_chunks > 0
    before = conn.execute("SELECT * FROM embeddings ORDER BY event_id, chunk_idx").fetchall()
    s2 = embed_events(conn, emb)
    assert s2.chunks_embedded == 0 and s2.chunks_unchanged == s1.total_chunks
    s3 = embed_events(conn, emb, rebuild=True)
    assert s3.chunks_embedded == s1.total_chunks
    after = conn.execute("SELECT * FROM embeddings ORDER BY event_id, chunk_idx").fetchall()
    assert [tuple(r) for r in before] == [tuple(r) for r in after]
    row = after[0]
    assert row["model"] == emb.name and row["dim"] == emb.dim
    assert len(row["vector"]) == 4 * emb.dim and len(row["text_hash"]) == 64


def test_embed_reuses_vectors_for_identical_text(conn, emb):
    embed_events(conn, emb)
    conn.execute(
        "INSERT INTO events(source, source_id, ts, kind, text, token_estimate, content_hash)"
        " SELECT source, source_id || '-dup', ts, kind, text, token_estimate, content_hash"
        " FROM events WHERE id = 1"
    )
    stats = embed_events(conn, emb)
    assert stats.chunks_embedded == 0 and stats.chunks_reused == 1


def test_embeddings_never_see_unredacted_text(conn):
    secret = "sk-abcdefghijklmnopqrstuvwx1234"
    conn.execute(  # a raw row that bypassed ingest-time redaction (defense in depth)
        "INSERT INTO events(source, source_id, ts, kind, text, token_estimate, content_hash)"
        " VALUES ('x', 'leak', 1, 'text', ?, 1, 'h')",
        (f"the key is {secret} keep it safe",),
    )
    seen: list[str] = []

    class Recorder(FakeEmbedder):
        def embed(self, texts):
            seen.extend(texts)
            return super().embed(texts)

    embed_events(conn, Recorder())
    assert seen and not any(secret in t for t in seen)
    assert (
        conn.execute(
            "SELECT count(*) FROM embeddings WHERE text_hash = ?",
            (text_hash(f"the key is {secret} keep it safe"),),
        ).fetchone()[0]
        == 0
    )


def test_embed_does_not_touch_events(conn, emb):
    snapshot = conn.execute("SELECT * FROM events ORDER BY id").fetchall()
    embed_events(conn, emb)
    assert [tuple(r) for r in snapshot] == [
        tuple(r) for r in conn.execute("SELECT * FROM events ORDER BY id")
    ]


# --- retrieval --------------------------------------------------------------------------


def test_modes_paraphrase_needs_vector(embedded, emb):
    q = "why were users getting signed out so quickly"  # FTS ANDs every word: no match
    assert retrieve(embedded, q, mode="fts", include_all=True) == []
    assert (
        sessions(retrieve(embedded, q, mode="vector", embedder=emb, include_all=True))[0] == "cc1"
    )
    assert (
        sessions(retrieve(embedded, q, mode="hybrid", embedder=emb, include_all=True))[0] == "cc1"
    )


def test_hybrid_is_rrf_of_both_rankers(embedded, emb):
    q = "webhooks/dispatcher.py"
    fts = [h.citation.event_id for h in search(embedded, q, include_all=True, limit=50)]
    hybrid = retrieve(embedded, q, mode="hybrid", embedder=emb, include_all=True, limit=5)
    assert hybrid[0].citation.event_id == fts[0]  # in both lists at rank 1
    assert hybrid[0].snippet.count("«")  # FTS highlighted snippet kept
    ranks = [h.rank for h in hybrid]
    assert ranks == sorted(ranks)  # lower is better, best first
    assert hybrid[0].rank == pytest.approx(-2 / 61, rel=0.2)  # ~2/(60+1) if first in both


def test_filters_apply_identically_in_every_mode(embedded, emb):
    q = "dashboard filter typing sluggish retry cache"
    for mode in ("fts", "vector", "hybrid"):
        kw = {"mode": mode, "embedder": emb, "limit": 50}
        assert {h.citation.source for h in retrieve(embedded, q, source="codex", **kw)} <= {"codex"}
        assert {h.citation.project_id for h in retrieve(embedded, q, project="beta", **kw)} <= {
            "beta"
        }
        # web chats are unlinked: hidden unless include_all, in every mode
        assert "chatgpt" not in {h.citation.source for h in retrieve(embedded, q, **kw)}
        assert {
            h.citation.ts for h in retrieve(embedded, q, until=0, include_all=True, **kw)
        } == set()
    embedded.execute("UPDATE projects SET mcp_exclude = 1 WHERE id = 'beta'")
    embedded.execute("UPDATE projects SET allowed = 0 WHERE id = 'alpha'")
    vec = retrieve(embedded, q, mode="vector", embedder=emb, exclude_mcp=True, limit=50)
    assert vec == []  # alpha not allowed, beta mcp-excluded
    since = retrieve(embedded, q, mode="vector", embedder=emb, include_all=True, since=10**15)
    assert since == []


def test_vector_without_embeddings_or_extra(conn, emb, monkeypatch):
    with pytest.raises(NoEmbeddings, match="ctx embed"):
        retrieve(conn, "x", mode="vector", embedder=emb)
    monkeypatch.setenv("CTX_EMBED_MODEL", "nomodel:x")  # never load a real model in tests
    with pytest.raises(EmbedderUnavailable, match="unavailable"):
        resolve_mode(conn, "hybrid", None)
    with pytest.raises(ValueError, match="unknown mode"):
        resolve_mode(conn, "bogus")


def test_auto_mode(conn, emb, monkeypatch):
    assert resolve_mode(conn, "auto") == ("fts", None, None)  # no embeddings: silent fts
    monkeypatch.setenv("CTX_EMBED_MODEL", "fake:trigram")
    embed_events(conn, get_embedder())
    mode, embedder, note = resolve_mode(conn, "auto")
    assert mode == "hybrid" and embedder.name == "fake:trigram" and note is None
    hits = retrieve(conn, "why were users getting signed out so quickly", include_all=True)
    assert sessions(hits)[0] == "cc1"  # auto == hybrid here
    monkeypatch.setenv("CTX_EMBED_MODEL", "nomodel:x")  # configured model has no rows: fts
    assert resolve_mode(conn, "auto")[0] == "fts"
    monkeypatch.setattr(
        "ctx.core.retrieve.get_embedder",
        lambda spec=None: (_ for _ in ()).throw(EmbedderUnavailable("model not cached")),
    )
    conn.execute("UPDATE embeddings SET model = 'nomodel:x'")
    mode, embedder, note = resolve_mode(conn, "auto")
    assert mode == "fts" and embedder is None and "model not cached" in note


def test_build_context_and_mcp_use_the_same_retrieval(embedded, emb, monkeypatch, tmp_path):
    q = "why were users getting signed out so quickly"
    assert build_context(embedded, q, 500, include_all=True, mode="fts").items == []
    pkg = build_context(embedded, q, 500, include_all=True, embedder=emb)
    assert pkg.items and pkg.items[0].citation.source_session_id == "cc1"
    # MCP: real DB under CTX_HOME, default model resolved from the environment
    from ctx.core import paths

    c = build_eval_db(tmp_path / "mcp", db_file=paths.db_path())
    monkeypatch.setenv("CTX_EMBED_MODEL", "fake:trigram")
    embed_events(c, get_embedder())
    c.close()
    out = text_of(call("search", {"query": q}))
    assert "session=cc1" in out
    ctx_out = text_of(call("get_context", {"query": q, "budget_tokens": 400}))
    assert "session=cc1" in ctx_out


# --- local_only (rule 9) ----------------------------------------------------------------


def test_cloud_embedder_never_sees_local_only_projects(conn):
    cloud = FakeCloudEmbedder()
    stats = embed_events(conn, cloud)
    assert stats.total_chunks == 0 and cloud.seen == []  # every project starts local_only=1
    conn.execute("UPDATE projects SET local_only = 0 WHERE id = 'beta'")
    stats = embed_events(conn, cloud)
    assert stats.total_chunks > 0 and stats.events_skipped_local_only > 0
    rows = conn.execute(
        "SELECT DISTINCT COALESCE(s.project_id, e.project_id) AS p FROM embeddings em"
        " JOIN events e ON e.id = em.event_id LEFT JOIN sessions s ON s.id = e.session_id"
    ).fetchall()
    assert [r["p"] for r in rows] == ["beta"]
    assert all("jitter" not in t and "SQLite" not in t for t in cloud.seen)


def test_cloud_embedder_query_requires_non_local_project(conn):
    cloud = FakeCloudEmbedder()
    conn.execute("UPDATE projects SET local_only = 0 WHERE id = 'beta'")
    embed_events(conn, cloud)
    cloud.seen.clear()
    for project in (None, "alpha"):
        with pytest.raises(LocalOnlyViolation):
            retrieve(conn, "dashboard", mode="vector", embedder=cloud, project=project)
    assert cloud.seen == []  # the query never reached the cloud embedder
    hits = retrieve(
        conn, "dashboard filter", mode="hybrid", embedder=cloud, project="beta", include_all=True
    )
    assert hits and cloud.seen == ["dashboard filter"]
