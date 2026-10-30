"""Retrieval eval harness (M4): score `retrieve` against a hand-written question set.

Eval file = JSONL, one question per line (blank lines and `#` comment lines ignored):

    {"id": "q1", "question": "...", "filters": {"project": "..", "source": "..",
     "since": "30d", "until": "2026-10-31"}, "expect": [{"source": "claude_code",
     "session_id": "s1", "message_id": "m3"}], "expect_contains": ["optional substring"]}

`expect` items are citations (`session_id` = the source's own session id; `message_id`
optional: omitted means any event of that session counts). A hit is relevant when it matches
an expected citation; each expected citation is credited once (a second hit on the same
target is a non-relevant duplicate for nDCG/recall). Metrics are binary-relevance
recall@k, MRR and nDCG@k, averaged over questions. `expect_contains` is reported
separately as `contains@k`: the share of substrings found in the redacted text of the top-k.
"""

import json
import math
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from ctx.core.embed import Embedder
from ctx.core.retrieve import resolve_mode, retrieve
from ctx.core.search import SearchHit, parse_when

FILTER_KEYS = {"project", "source", "since", "until"}


class EvalFileError(ValueError):
    pass


@dataclass(frozen=True)
class Expected:
    source: str
    session_id: str
    message_id: str | None = None

    def matches(self, hit: SearchHit) -> bool:
        c = hit.citation
        return (
            c.source == self.source
            and c.source_session_id == self.session_id
            and (self.message_id is None or c.message_id == self.message_id)
        )


@dataclass(frozen=True)
class Question:
    id: str
    question: str
    expect: list[Expected]
    filters: dict[str, str] = field(default_factory=dict)
    expect_contains: list[str] = field(default_factory=list)


def load_eval_set(path: Path) -> list[Question]:
    questions: list[Question] = []
    seen: set[str] = set()
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            raw = json.loads(line)
            q = _parse_question(raw, n)
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise EvalFileError(f"{path}:{n}: {type(exc).__name__}: {exc}") from exc
        if q.id in seen:
            raise EvalFileError(f"{path}:{n}: duplicate id {q.id!r}")
        seen.add(q.id)
        questions.append(q)
    if not questions:
        raise EvalFileError(f"{path}: no questions")
    return questions


def _parse_question(raw: dict, n: int) -> Question:
    if not isinstance(raw, dict):
        raise TypeError("each line must be a JSON object")
    filters = raw.get("filters") or {}
    unknown = set(filters) - FILTER_KEYS
    if unknown:
        raise TypeError(f"unknown filter(s) {sorted(unknown)}; allowed: {sorted(FILTER_KEYS)}")
    expect = [
        Expected(e["source"], e["session_id"], e.get("message_id")) for e in raw.get("expect", [])
    ]
    if not expect:
        raise TypeError("'expect' needs at least one {source, session_id[, message_id]}")
    return Question(
        id=str(raw.get("id") or f"q{n}"),
        question=raw["question"],
        expect=expect,
        filters={k: str(v) for k, v in filters.items()},
        expect_contains=list(raw.get("expect_contains") or []),
    )


@dataclass
class QuestionScore:
    id: str
    recall: float
    rr: float
    ndcg: float
    contains: float | None
    first_rank: int | None


@dataclass
class ModeReport:
    mode: str
    k: int
    recall: float
    mrr: float
    ndcg: float
    contains: float | None
    questions: list[QuestionScore]


def score_question(q: Question, hits: list[SearchHit], k: int) -> QuestionScore:
    top = hits[:k]
    credited: set[int] = set()
    gains: list[int] = []
    for hit in top:
        gain = 0
        for i, e in enumerate(q.expect):
            if i not in credited and e.matches(hit):
                credited.add(i)
                gain = 1
                break
        gains.append(gain)
    dcg = sum(g / math.log2(r + 2) for r, g in enumerate(gains))
    ideal = sum(1 / math.log2(r + 2) for r in range(min(len(q.expect), k)))
    first = next((r + 1 for r, g in enumerate(gains) if g), None)
    contains = None
    if q.expect_contains:
        blob = "\n".join(h.text or "" for h in top).lower()
        contains = sum(s.lower() in blob for s in q.expect_contains) / len(q.expect_contains)
    return QuestionScore(
        q.id,
        len(credited) / len(q.expect),
        1 / first if first else 0.0,
        dcg / ideal,
        contains,
        first,
    )


def run_eval(
    conn: sqlite3.Connection,
    questions: list[Question],
    *,
    mode: str,
    k: int = 10,
    embedder: Embedder | None = None,
    include_all: bool = False,
) -> ModeReport:
    """Run every question through `retrieve` in one mode. vector/hybrid raise (with an
    actionable message) when embeddings are missing; the CLI turns that into a skip."""
    mode, embedder, _ = resolve_mode(conn, mode, embedder)
    scores = []
    for q in questions:
        f = q.filters
        hits = retrieve(
            conn,
            q.question,
            mode=mode,
            embedder=embedder,
            project=f.get("project"),
            source=f.get("source"),
            since=parse_when(f["since"]) if "since" in f else None,
            until=parse_when(f["until"], end_of_day=True) if "until" in f else None,
            include_all=include_all,
            limit=k,
            include_text=bool(q.expect_contains),
        )
        scores.append(score_question(q, hits, k))
    n = len(scores)
    with_contains = [s.contains for s in scores if s.contains is not None]
    return ModeReport(
        mode=mode,
        k=k,
        recall=sum(s.recall for s in scores) / n,
        mrr=sum(s.rr for s in scores) / n,
        ndcg=sum(s.ndcg for s in scores) / n,
        contains=sum(with_contains) / len(with_contains) if with_contains else None,
        questions=scores,
    )
