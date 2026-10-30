"""Evidence-linked summaries of episodes and days (M5), stored in `summaries`.

The LLM only writes prose. Which events a summary covers is decided deterministically
(rule 7): an episode's `episode_events`, or the events of one local day and project; the
text is built by `build_context`, and every event that reached the prompt is linked in
`summary_events` (`cited=1` where the model also cited it inline and the id was valid).
A summary with a hallucinated or missing citation is not stored. Provider, model and
prompt version are recorded; re-running with identical inputs is a no-op unless forced.
"""

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ctx.core.context import PROMPT_VERSION, build_context
from ctx.llm.grounded import Grounded, package_scope, run_grounded
from ctx.llm.policy import cloud_scope_refusal
from ctx.llm.providers import Provider

TASK = (
    "Summarize this work in 3 to 6 short bullet points: what was done, what was decided, what "
    "is unfinished. Every bullet must cite its evidence ids."
)


@dataclass
class SummaryResult:
    status: str  # stored | unchanged | skipped | rejected
    scope: str
    scope_key: str
    project_id: str | None
    summary_id: int | None = None
    text: str = ""
    detail: str = ""
    grounded: Grounded | None = None
    warnings: list[str] = field(default_factory=list)


def _input_hash(event_ids: list[int], provider: Provider) -> str:
    blob = json.dumps([PROMPT_VERSION, provider.name, provider.model, sorted(event_ids)])
    return hashlib.sha256(blob.encode()).hexdigest()


def summarize_group(
    conn: sqlite3.Connection,
    provider: Provider,
    *,
    scope: str,
    scope_key: str,
    project_id: str | None,
    event_ids: list[int],
    budget: int,
    include_all: bool,
    force: bool = False,
    skip_refused: bool = False,
) -> SummaryResult:
    res = SummaryResult("skipped", scope, scope_key, project_id)
    package = build_context(
        conn, "", budget, project_id, include_all=include_all, event_ids=event_ids
    )
    if not package.items:
        res.detail = "no visible events (see `--all`, project allowed flag)"
        return res
    if skip_refused and provider.is_cloud:
        why = cloud_scope_refusal(conn, package_scope(conn, package, project_id))
        if why:
            res.detail = why
            return res
    ids = package.evidence_event_ids
    digest = _input_hash(ids, provider)
    key = (scope, scope_key, project_id or "")
    old = conn.execute(
        "SELECT id, text, input_hash FROM summaries WHERE scope=? AND scope_key=? AND project_id=?",
        key,
    ).fetchone()
    if old and old["input_hash"] == digest and not force:
        return SummaryResult(
            "unchanged", scope, scope_key, project_id, old["id"], old["text"], "up to date"
        )
    g = run_grounded(conn, provider, package, TASK, project=project_id)
    res.grounded, res.text, res.warnings = g, g.text, g.warnings()
    if not g.ok or g.insufficient:
        res.status = "rejected"
        res.detail = "not stored: " + ("; ".join(res.warnings) or "model reported no evidence")
        return res
    now = datetime.now(UTC).isoformat(timespec="seconds")
    cited = set(g.cited)
    conn.execute("BEGIN")
    try:
        conn.execute("DELETE FROM summaries WHERE scope=? AND scope_key=? AND project_id=?", key)
        cur = conn.execute(
            "INSERT INTO summaries(scope, scope_key, project_id, start_ts, end_ts, text,"
            " provider, model, prompt_version, input_hash, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                *key,
                min(i.citation.ts for i in package.items),
                max(i.citation.ts for i in package.items),
                g.text,
                provider.name,
                provider.model,
                PROMPT_VERSION,
                digest,
                now,
            ),
        )
        res.summary_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO summary_events(summary_id, event_id, cited) VALUES (?, ?, ?)",
            [(res.summary_id, eid, int(eid in cited)) for eid in ids],
        )
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    res.status = "stored"
    return res


def summarize_episode(
    conn: sqlite3.Connection,
    provider: Provider,
    episode_id: int,
    *,
    budget: int = 4000,
    include_all: bool = False,
    force: bool = False,
) -> SummaryResult:
    ep = conn.execute("SELECT * FROM episodes WHERE id = ?", (episode_id,)).fetchone()
    if ep is None:
        raise ValueError(f"unknown episode {episode_id} (run `ctx reindex`; see `ctx timeline`)")
    ids = [
        r["event_id"]
        for r in conn.execute(
            "SELECT event_id FROM episode_events WHERE episode_id = ? ORDER BY event_id",
            (episode_id,),
        )
    ]
    return summarize_group(
        conn,
        provider,
        scope="episode",
        scope_key=str(ep["start_ts"]),
        project_id=ep["project_id"],
        event_ids=ids,
        budget=budget,
        include_all=include_all or ep["project_id"] is None,
        force=force,
    )


def summarize_day(
    conn: sqlite3.Connection,
    provider: Provider,
    day: str,
    lo: int,
    hi: int,
    *,
    project: str | None = None,
    budget: int = 4000,
    include_all: bool = False,
    force: bool = False,
) -> list[SummaryResult]:
    """One summary per (day, project) so each is gated on its own project; a cloud provider
    skips (not fails) the local_only groups."""
    from ctx.core.search import resolve_project

    only = resolve_project(conn, project) if project else None
    groups: dict[str | None, list[int]] = {}
    for r in conn.execute(
        "SELECT e.id, COALESCE(s.project_id, e.project_id) AS pid FROM events e"
        " LEFT JOIN sessions s ON s.id = e.session_id WHERE e.ts BETWEEN ? AND ? ORDER BY e.id",
        (lo, hi),
    ):
        if only is None or r["pid"] == only:
            groups.setdefault(r["pid"], []).append(r["id"])
    return [
        summarize_group(
            conn,
            provider,
            scope="day",
            scope_key=day,
            project_id=pid,
            event_ids=ids,
            budget=budget,
            include_all=include_all,
            force=force,
            skip_refused=True,
        )
        for pid, ids in sorted(groups.items(), key=lambda kv: kv[0] or "")
    ]
