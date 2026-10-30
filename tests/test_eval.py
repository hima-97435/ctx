"""Eval harness: file format, metrics, and the synthetic multi-source set across modes."""

import importlib.util
import json
import math

import pytest
from typer.testing import CliRunner

from ctx.cli.main import app
from ctx.core import paths
from ctx.core.embed import embed_events, get_embedder
from ctx.core.evalset import (
    EvalFileError,
    Expected,
    Question,
    load_eval_set,
    run_eval,
    score_question,
)
from ctx.core.search import Citation, SearchHit
from tests.fake_embedder import FakeEmbedder
from tests.fixtures.eval_corpus import EVAL_SET_PATH, build_eval_db

runner = CliRunner()


def hit(session, message=None, source="s", text=""):
    c = Citation(source, f"{source}:{session}", session, message, 1, "id", 0, None)
    return SearchHit(c, "text", None, "", 0.0, 1, text)


def test_score_question_metrics():
    q = Question("q", "?", [Expected("s", "a"), Expected("s", "b", "m2")], expect_contains=["foo"])
    hits = [hit("x"), hit("a", "m1", text="Foo"), hit("a", "m9"), hit("b", "m1"), hit("b", "m2")]
    sc = score_question(q, hits, k=10)
    assert sc.first_rank == 2 and sc.rr == 0.5
    assert sc.recall == 1.0  # a (any message) at 2, b/m2 at 5; duplicate "a" at 3 earns nothing
    ideal = 1 + 1 / math.log2(3)
    assert sc.ndcg == pytest.approx((1 / math.log2(3) + 1 / math.log2(6)) / ideal)
    assert sc.contains == 1.0
    assert score_question(q, hits, k=1).recall == 0.0
    assert score_question(q, [], k=5).ndcg == 0.0


def test_load_eval_set_validation(tmp_path):
    f = tmp_path / "e.jsonl"
    f.write_text('# c\n\n{"question": "q", "expect": [{"source": "s", "session_id": "x"}]}\n')
    (q,) = load_eval_set(f)
    assert q.id == "q3" and q.expect == [Expected("s", "x", None)]
    for bad in (
        "not json",
        '{"question": "q", "expect": []}',
        '{"question": "q", "expect": [{"source": "s", "session_id": "x"}], "filters": {"x": 1}}',
        '{"expect": [{"source": "s", "session_id": "x"}]}',
    ):
        f.write_text(bad)
        with pytest.raises(EvalFileError):
            load_eval_set(f)
    f.write_text("# nothing\n")
    with pytest.raises(EvalFileError, match="no questions"):
        load_eval_set(f)


def test_synthetic_set_shape():
    qs = load_eval_set(EVAL_SET_PATH)
    assert len(qs) == 20 and len({q.id for q in qs}) == 20
    assert {q.id.split("-")[0] for q in qs} == {"para", "err", "file", "why"}


def test_synthetic_set_compares_modes(tmp_path):
    conn = build_eval_db(tmp_path)
    emb = FakeEmbedder()
    embed_events(conn, emb)
    qs = load_eval_set(EVAL_SET_PATH)
    r = {
        m: run_eval(conn, qs, mode=m, embedder=emb, include_all=True)
        for m in ("fts", "vector", "hybrid")
    }
    assert r["fts"].recall == pytest.approx(0.5)  # AND-only FTS misses every paraphrase/why
    assert r["hybrid"].recall == 1.0 and r["vector"].recall == 1.0
    assert r["hybrid"].mrr >= r["fts"].mrr + 0.3
    assert r["hybrid"].ndcg > r["fts"].ndcg
    assert all(r[m].k == 10 for m in r)


def test_cli_eval_side_by_side(tmp_path, monkeypatch):
    build_eval_db(tmp_path, db_file=paths.db_path()).close()
    monkeypatch.setenv("CTX_EMBED_MODEL", "fake:trigram")
    out = runner.invoke(app, ["eval", "run", str(EVAL_SET_PATH), "--all"]).output
    assert "fts: " not in out and "vector: skipped" in out and "hybrid: skipped" in out
    assert runner.invoke(app, ["embed"]).exit_code == 0
    res = runner.invoke(app, ["eval", "run", str(EVAL_SET_PATH), "--all", "--json", "--k", "5"])
    data = json.loads(res.output)
    assert [m["mode"] for m in data["modes"]] == ["fts", "vector", "hybrid"] and data["k"] == 5
    table = runner.invoke(app, ["eval", "run", str(EVAL_SET_PATH), "--all"]).output
    assert "RECALL@K" in table and "para-signed-out" in table
    one = runner.invoke(app, ["eval", "run", str(EVAL_SET_PATH), "--mode", "fts", "--all"])
    assert one.exit_code == 0 and "hybrid" not in one.output
    bad = runner.invoke(app, ["eval", "run", str(EVAL_SET_PATH), "--mode", "nope"])
    assert bad.exit_code == 2


def test_cli_eval_explicit_vector_without_embeddings_errors(tmp_path, monkeypatch):
    build_eval_db(tmp_path, db_file=paths.db_path()).close()
    monkeypatch.setenv("CTX_EMBED_MODEL", "fake:trigram")
    res = runner.invoke(app, ["eval", "run", str(EVAL_SET_PATH), "--mode", "hybrid"])
    assert res.exit_code == 2 and "ctx embed" in res.output


REAL = importlib.util.find_spec("model2vec") is not None


def _real_model_cached() -> bool:
    if not REAL:
        return False
    try:
        from huggingface_hub import try_to_load_from_cache

        found = try_to_load_from_cache("minishlab/potion-base-8M", "config.json")
        return isinstance(found, str)
    except Exception:
        return False


@pytest.mark.skipif(not _real_model_cached(), reason="model2vec + cached potion-base-8M needed")
def test_real_local_model_beats_fts_on_synthetic_set(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")  # cached or skip; never download in tests
    conn = build_eval_db(tmp_path)
    emb = get_embedder()
    embed_events(conn, emb)
    qs = load_eval_set(EVAL_SET_PATH)
    fts = run_eval(conn, qs, mode="fts", include_all=True)
    hyb = run_eval(conn, qs, mode="hybrid", embedder=emb, include_all=True)
    assert hyb.recall > fts.recall and hyb.mrr > fts.mrr
