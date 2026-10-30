"""ctx ask, ctx handoff (+ MCP tool) and ctx summarize, with scripted providers."""

import asyncio
import json

import pytest
from mcp import Client
from typer.testing import CliRunner

from ctx.cli.main import app
from ctx.core import paths
from ctx.core.context import assemble_prompt, build_context
from ctx.core.db import connect
from ctx.core.episodes import rebuild_episodes
from ctx.core.handoff import collect, render_handoff
from ctx.core.projects import ProjectConfig, sync_projects
from ctx.llm.grounded import ask, extract_citation_ids, ground
from ctx.llm.http import LLMError
from ctx.llm.policy import CloudRefused
from ctx.llm.synthesis import summarize_day, summarize_episode
from ctx.mcp.server import build_server
from tests.llm_helpers import SECRET, ScriptedProvider

runner = CliRunner()
T0 = 1_792_000_000_000  # fixed epoch ms
MIN = 60_000

ROWS = [  # (offset min, role, kind, text)
    (0, "user", "text", "Please add retry logic to the sync engine."),
    (1, "assistant", "text", "We decided to use exponential backoff instead of fixed sleeps."),
    (2, "assistant", "tool:edit", 'input: {"file_path": "/repo/src/sync.py"}'),
    (3, "assistant", "tool:bash", 'input: {"command": "pytest -q tests/test_sync.py"}'),
    (4, "assistant", "tool:bash", 'input: {"command": "pytest -q tests/test_sync.py"}'),
    (5, "assistant", "text", "TODO: still need to cover the 429 Retry-After header."),
    (6, "user", "text", "Ok, continue with the Retry-After handling next."),
    (200, "user", "text", "Later session: rename the zebra module."),
    (201, "assistant", "text", f"Renamed. Remember key {SECRET} must never be shown."),
]


def add(conn, project, sid, rows, source="opencode"):
    full = f"{source}:{sid}"
    conn.execute(
        "INSERT INTO sessions(id, source, source_session_id, project_id, started_at, title)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (full, source, sid, project, T0, f"title {sid}"),
    )
    for i, (off, role, kind, text) in enumerate(rows):
        conn.execute(
            "INSERT INTO events(source, source_id, ts, project_id, session_id, role, kind,"
            " text, token_estimate, content_hash, message_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '0', ?)",
            (source, f"{sid}-{i}", T0 + off * MIN, project, full, role, kind, text, 10, f"m{i}"),
        )


@pytest.fixture()
def db():
    conn = connect(paths.db_path())
    sync_projects(
        conn,
        [
            ProjectConfig(id="pub", name="Pub", allowed=True, local_only=False),
            ProjectConfig(id="priv", name="Priv", allowed=True, local_only=True),
            ProjectConfig(id="hid", name="Hid", allowed=True, local_only=False, mcp_exclude=True),
        ],
    )
    add(conn, "pub", "s1", ROWS)
    add(conn, "priv", "s2", [(0, "user", "text", "private retry notes")])
    add(conn, "hid", "s3", [(0, "user", "text", "excluded retry notes")])
    rebuild_episodes(conn)
    yield conn
    conn.close()


def ids_of(package):
    return {i.citation.event_id for i in package.items}


# --- citations / prompt ---


def test_extract_citation_ids_forms():
    assert extract_citation_ids("a [E3] b [E12][E3] [E4, E5] [x] E9 [e7]") == [3, 12, 4, 5]


def test_ground_flags_hallucinated_and_uncited(db):
    package = build_context(db, "retry", 2000, "pub")
    real = sorted(ids_of(package))[0]
    g = ground(f"ok [E{real}] and made up [E999999]", package)
    assert g.cited == [real] and g.hallucinated == [999999] and not g.ok
    assert "999999" in g.warnings()[0]
    assert not ground("no citations at all", package).ok
    refusal = ground("INSUFFICIENT EVIDENCE", package)
    assert refusal.ok and refusal.insufficient
    assert not ground("INSUFFICIENT EVIDENCE [E999999]", package).ok


def test_prompt_assembly_lists_ids_and_redacts(db):
    package = build_context(db, "renamed key", 2000, "pub")
    msgs = assemble_prompt(package, "t")
    body = msgs[1]["content"]
    assert all(f"[E{i}]" in body for i in ids_of(package))
    assert SECRET not in body and "ONLY the evidence" in msgs[0]["content"]


# --- ask ---


def test_ask_answers_with_valid_citations(db):
    package = build_context(db, "retry logic", 2000, "pub")
    eid = sorted(ids_of(package))[0]
    p = ScriptedProvider(f"Backoff was chosen [E{eid}].", name="ollama", is_cloud=False)
    g = ask(db, p, package, project="pub")
    assert g.ok and [c.event_id for c in g.citations] == [eid]
    assert len(p.calls) == 1


def test_ask_without_evidence_does_not_call_llm(db):
    package = build_context(db, "nonexistentterm", 2000, "pub")
    p = ScriptedProvider("should not be used")
    g = ask(db, p, package, project="pub")
    assert g.insufficient and g.ok and p.calls == []


def test_ask_cli_outputs_citations_and_flags_hallucination(db, monkeypatch):
    package = build_context(db, "retry logic", 2000, "pub")
    eid = sorted(ids_of(package))[0]
    monkeypatch.setattr(
        "ctx.cli.main.get_provider",
        lambda *a, **k: ScriptedProvider(f"Yes [E{eid}]", name="ollama", is_cloud=False),
    )
    r = runner.invoke(app, ["ask", "retry logic", "--project", "pub"])
    assert r.exit_code == 0, r.output
    assert f"[E{eid}] opencode session=s1" in r.output
    monkeypatch.setattr(
        "ctx.cli.main.get_provider",
        lambda *a, **k: ScriptedProvider(f"Yes [E{eid}] [E424242]", is_cloud=False),
    )
    r = runner.invoke(app, ["ask", "retry logic", "--project", "pub"])
    assert r.exit_code == 1 and "hallucinated" in r.output and "E424242" in r.output


def test_ask_cli_refuses_cloud_for_local_only_project(db, monkeypatch):
    p = ScriptedProvider("x [E1]")
    monkeypatch.setattr("ctx.cli.main.get_provider", lambda *a, **k: p)
    r = runner.invoke(app, ["ask", "retry notes", "--project", "priv"])
    assert r.exit_code == 1 and "refused" in r.output and p.calls == []


def test_ask_cli_provider_errors_are_clean(db, monkeypatch):
    class Boom(ScriptedProvider):
        def chat(self, *a, **k):
            raise LLMError("gemini: HTTP 500 from https://h: boom")

    monkeypatch.setattr("ctx.cli.main.get_provider", lambda *a, **k: Boom("x", is_cloud=False))
    r = runner.invoke(app, ["ask", "retry logic", "--project", "pub"])
    assert r.exit_code == 1 and "HTTP 500" in r.output and "Traceback" not in r.output


def test_ask_cli_unknown_provider(db):
    assert runner.invoke(app, ["ask", "q", "--provider", "skynet"]).exit_code == 2


# --- handoff ---


def brief(db, **kw):
    to = kw.pop("to", "generic")
    return render_handoff(db, collect(db, "pub", **kw), to)


def test_handoff_content_and_citations_resolve(db):
    text = brief(db)
    assert "# Handoff: Pub" in text
    assert "exponential backoff instead of fixed sleeps" in text  # decision
    assert "429 Retry-After" in text  # open thread
    assert "`/repo/src/sync.py` (1x)" in text
    assert "`pytest -q tests/test_sync.py` (2x)" in text
    assert "rename the zebra module" in text  # last request
    assert SECRET not in text and "[REDACTED" in text
    package = collect(db, "pub")
    cited = {int(x) for x in __import__("re").findall(r"^- \[E(\d+)\]", text, flags=8)}
    assert cited and cited <= ids_of(package)
    assert "private" not in text and "excluded" not in text


def test_handoff_is_deterministic_and_sends_nothing(db, monkeypatch):
    def no_llm(*a, **k):
        raise AssertionError("deterministic handoff must not touch a provider")

    monkeypatch.setattr("ctx.llm.providers.get_provider", no_llm)
    assert brief(db) == brief(db)
    assert brief(db, to="codex") != brief(db, to="claude-code")
    r1 = runner.invoke(app, ["handoff", "--project", "pub"])
    r2 = runner.invoke(app, ["handoff", "--project", "pub"])
    assert r1.exit_code == 0 and r1.output == r2.output == brief(db)


def test_handoff_since_and_empty_and_bad_target(db):
    assert "No activity found" in render_handoff(
        db, collect(db, "pub", since=T0 + 10_000 * MIN), "generic"
    )
    r = runner.invoke(app, ["handoff", "--project", "pub", "--to", "vim"])
    assert r.exit_code == 1
    assert runner.invoke(app, ["handoff", "--project", "pub", "--since", "bogus"]).exit_code == 1


def test_handoff_llm_mode_gated_and_checked(db, monkeypatch):
    cloud = ScriptedProvider("brief [E1]")
    monkeypatch.setattr("ctx.cli.main.get_provider", lambda *a, **k: cloud)
    r = runner.invoke(app, ["handoff", "--llm", "--project", "priv"])
    assert r.exit_code == 1 and "refused" in r.output and cloud.calls == []
    eid = sorted(ids_of(collect(db, "pub")))[0]
    local = ScriptedProvider(f"# Brief [E{eid}]", name="ollama", is_cloud=False)
    monkeypatch.setattr("ctx.cli.main.get_provider", lambda *a, **k: local)
    r = runner.invoke(app, ["handoff", "--llm", "--project", "pub"])
    assert r.exit_code == 0 and f"[E{eid}]" in r.output and len(local.calls) == 1
    bad = ScriptedProvider("# Brief [E31337]", is_cloud=False)
    monkeypatch.setattr("ctx.cli.main.get_provider", lambda *a, **k: bad)
    assert runner.invoke(app, ["handoff", "--llm", "--project", "pub"]).exit_code == 1


def mcp_call(tool, args):
    async def go():
        async with Client(build_server()) as c:
            return await c.call_tool(tool, args)

    res = asyncio.run(go())
    return res.is_error, "\n".join(c.text for c in res.content)


def test_mcp_handoff_visibility_and_determinism(db):
    err, out = mcp_call("handoff", {"project": "pub", "to": "claude-code"})
    assert not err and "# Handoff: Pub" in out and SECRET not in out
    assert mcp_call("handoff", {"project": "pub", "to": "claude-code"})[1] == out
    assert mcp_call("handoff", {"project": "hid"})[0]  # mcp_exclude looks nonexistent
    assert mcp_call("handoff", {"project": "pub", "to": "vim"})[0]
    err, out = mcp_call("handoff", {})
    assert not err and "excluded" not in out and "_10 events" in out


# --- summaries ---


def episode_id(db, project="pub"):
    return db.execute(
        "SELECT id FROM episodes WHERE project_id = ? ORDER BY start_ts LIMIT 1", (project,)
    ).fetchone()[0]


def test_summarize_episode_stores_links_and_records_versions(db):
    ep = episode_id(db)
    ids = [
        r[0] for r in db.execute("SELECT event_id FROM episode_events WHERE episode_id=?", (ep,))
    ]
    p = ScriptedProvider(f"- backoff [E{ids[0]}]", name="ollama", is_cloud=False, model="llama-x")
    res = summarize_episode(db, p, ep)
    assert res.status == "stored"
    row = db.execute("SELECT * FROM summaries").fetchone()
    assert (row["provider"], row["model"], row["prompt_version"]) == ("ollama", "llama-x", "p1")
    links = {
        r["event_id"]: r["cited"]
        for r in db.execute("SELECT * FROM summary_events WHERE summary_id=?", (row["id"],))
    }
    assert set(links) == set(ids)  # every event in the prompt is linked, deterministically
    assert [k for k, v in links.items() if v] == [ids[0]]  # only the cited one is flagged
    assert SECRET not in row["text"]
    again = summarize_episode(db, p, ep)
    assert again.status == "unchanged" and len(p.calls) == 1
    forced = summarize_episode(db, p, ep, force=True)
    assert forced.status == "stored" and len(p.calls) == 2
    assert db.execute("SELECT count(*) FROM summaries").fetchone()[0] == 1


def test_summary_with_hallucinated_citation_is_not_stored(db):
    p = ScriptedProvider("- made up [E999999]", is_cloud=False)
    res = summarize_episode(db, p, episode_id(db))
    assert res.status == "rejected" and "hallucinated" in res.detail
    assert db.execute("SELECT count(*) FROM summaries").fetchone()[0] == 0


def test_summary_survives_reindex_and_is_dropped_with_events_never(db):
    ep = episode_id(db)
    eid = db.execute("SELECT event_id FROM episode_events WHERE episode_id=?", (ep,)).fetchone()[0]
    summarize_episode(db, ScriptedProvider(f"x [E{eid}]", is_cloud=False), ep)
    rebuild_episodes(db)  # derived episode tables rebuilt from events alone
    assert db.execute("SELECT count(*) FROM summaries").fetchone()[0] == 1
    db.execute("DROP TABLE summary_events")
    db.execute("DROP TABLE summaries")  # derived: dropping loses no source of truth
    assert db.execute("SELECT count(*) FROM events").fetchone()[0] > 0


def test_cloud_episode_summary_refused_for_local_only(db):
    p = ScriptedProvider("x [E1]")
    with pytest.raises(CloudRefused):
        summarize_episode(db, p, episode_id(db, "priv"))
    assert p.calls == []


def test_summarize_day_groups_by_project_and_skips_local_only_for_cloud(db):
    from datetime import UTC, datetime

    day = datetime.fromtimestamp(T0 / 1000, UTC).date().isoformat()
    lo, hi = T0 - 1, T0 + 24 * 60 * MIN
    first = db.execute("SELECT min(id) FROM events WHERE project_id='pub'").fetchone()[0]
    cloud = ScriptedProvider(f"- day [E{first}]")
    results = summarize_day(db, cloud, day, lo, hi)
    by = {r.project_id: r for r in results}
    assert by["pub"].status == "stored"
    assert by["priv"].status == "skipped" and "local_only" in by["priv"].detail
    assert by["hid"].status == "stored" or by["hid"].status == "rejected"
    assert len(cloud.calls) == 2  # pub and hid; the local_only group never reached the model
    assert all("private retry notes" not in m["content"] for c in cloud.calls for m in c)


def test_summarize_cli(db, monkeypatch):
    first = db.execute("SELECT min(id) FROM events WHERE project_id='pub'").fetchone()[0]
    p = ScriptedProvider(f"- did stuff [E{first}]", name="ollama", is_cloud=False)
    monkeypatch.setattr("ctx.cli.main.get_provider", lambda *a, **k: p)
    r = runner.invoke(app, ["summarize", "--episode", str(episode_id(db))])
    assert r.exit_code == 0 and "stored" in r.output and f"[E{first}]" in r.output
    assert runner.invoke(app, ["summarize"]).exit_code == 2
    assert runner.invoke(app, ["summarize", "--episode", "1", "--day", "today"]).exit_code == 2
    assert runner.invoke(app, ["summarize", "--episode", "9999"]).exit_code == 1
    r = runner.invoke(app, ["summarize", "--day", "2026-01-01"])
    assert r.exit_code == 0 and "no events" in r.output
    out = json.dumps(r.output)
    assert SECRET not in out
