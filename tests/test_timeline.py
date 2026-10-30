"""M2: deterministic episodes, timeline, project commands and the MCP timeline tool."""

import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from mcp import Client
from typer.testing import CliRunner

from ctx.cli.main import app
from ctx.core import paths
from ctx.core.db import connect
from ctx.core.episodes import (
    build_timeline,
    local_day_bounds,
    parse_day,
    parse_local,
    rebuild_episodes,
    render_timeline,
    resolve_tz,
    segment,
    tool_targets,
)
from ctx.core.projects import ProjectConfig, list_projects, sync_projects
from ctx.mcp.server import build_server

runner = CliRunner()
MIN = 60_000
BERLIN = ZoneInfo("Europe/Berlin")
NY = ZoneInfo("America/New_York")
FAKE_TOKEN = "sk-abcdefghijklmnopqrstuvwx1234"


def ms(y, mo, d, h=0, mi=0, tz=UTC):
    return int(datetime(y, mo, d, h, mi, tzinfo=tz).timestamp() * 1000)


T0 = ms(2026, 10, 27, 9, 0)  # a Tuesday, UTC


def add_event(conn, source, sid, n, ts, project, kind="text", text="hello", title=None):
    full = f"{source}:{sid}"
    conn.execute(
        "INSERT OR IGNORE INTO sessions(id, source, source_session_id, project_id,"
        " started_at, title) VALUES (?, ?, ?, ?, ?, ?)",
        (full, source, sid, project, ts, title or f"title {sid}"),
    )
    conn.execute(
        "INSERT INTO events(source, source_id, ts, project_id, session_id, role, kind,"
        " text, token_estimate, content_hash) VALUES (?, ?, ?, ?, ?, 'user', ?, ?, 1, '0')",
        (source, f"{sid}-{n}", ts, project, full, kind, text),
    )


@pytest.fixture()
def db():
    conn = connect(paths.db_path())
    sync_projects(
        conn,
        [
            ProjectConfig(id="proj", name="Proj", allowed=True),
            ProjectConfig(id="other", name="Other", allowed=True),
            ProjectConfig(id="secret", name="Secret", allowed=True, mcp_exclude=True),
            ProjectConfig(id="home", name="Home", allowed=False),
        ],
    )
    yield conn
    conn.close()


def episodes(conn):
    return [
        (r["id"], r["project_id"], r["start_ts"], r["end_ts"])
        for r in conn.execute("SELECT * FROM episodes ORDER BY id")
    ]


# ------------------------------------------------------------------ segmentation


def test_segment_gap_boundaries():
    gap = 45 * MIN
    assert len(segment([(0, 1), (gap - 1, 2)], gap)) == 1  # 44:59.999 joins
    assert len(segment([(0, 1), (gap, 2)], gap)) == 2  # exactly 45:00 splits
    assert len(segment([(0, 1), (gap + 1, 2)], gap)) == 2
    assert segment([], gap) == []
    assert segment([(5, 1), (5, 2)], gap) == [[(5, 1), (5, 2)]]  # same ms stays together


def test_episodes_split_by_gap_and_project(db):
    add_event(db, "opencode", "a", 0, T0, "proj")
    add_event(db, "opencode", "a", 1, T0 + 44 * MIN, "proj")  # joins
    add_event(db, "opencode", "a", 2, T0 + 89 * MIN, "proj")  # +45 min: splits
    add_event(db, "opencode", "b", 0, T0 + 10 * MIN, "other")
    assert rebuild_episodes(db) == 3
    eps = episodes(db)
    assert [(e[1], e[2], e[3]) for e in eps] == [
        ("proj", T0, T0 + 44 * MIN),
        ("other", T0 + 10 * MIN, T0 + 10 * MIN),
        ("proj", T0 + 89 * MIN, T0 + 89 * MIN),
    ]
    assert db.execute("SELECT COUNT(*) FROM episode_events").fetchone()[0] == 4


def test_configurable_gap(db):
    add_event(db, "opencode", "a", 0, T0, "proj")
    add_event(db, "opencode", "a", 1, T0 + 20 * MIN, "proj")
    assert rebuild_episodes(db, 45) == 1
    assert rebuild_episodes(db, 20) == 2  # exactly the gap splits
    with pytest.raises(ValueError):
        rebuild_episodes(db, 0)


def test_env_gap(db, monkeypatch):
    add_event(db, "opencode", "a", 0, T0, "proj")
    add_event(db, "opencode", "a", 1, T0 + 20 * MIN, "proj")
    monkeypatch.setenv("CTX_EPISODE_GAP_MINUTES", "10")
    assert rebuild_episodes(db) == 2
    monkeypatch.setenv("CTX_EPISODE_GAP_MINUTES", "abc")
    with pytest.raises(ValueError):
        rebuild_episodes(db)


def test_cross_source_merge_and_interleave(db):
    # Two sources, overlapping, same project: one episode. Alone each would split.
    for i in range(4):
        add_event(db, "opencode", "a", i, T0 + i * 40 * MIN, "proj")  # 0,40,80,120
    add_event(db, "claude_code", "c", 0, T0 + 20 * MIN, "proj")
    add_event(db, "claude_code", "c", 1, T0 + 60 * MIN, "proj")
    add_event(db, "claude_code", "c", 2, T0 + 100 * MIN, "proj")
    assert rebuild_episodes(db) == 1
    (ep,) = episodes(db)
    assert (ep[2], ep[3]) == (T0, T0 + 120 * MIN)


def test_session_project_wins_and_unlinked_bucket(db):
    add_event(db, "opencode", "a", 0, T0, None)  # event unlinked, session proj
    db.execute("UPDATE sessions SET project_id='proj' WHERE id='opencode:a'")
    add_event(db, "chatgpt", "w", 0, T0, None)  # web chat: no project
    add_event(db, "gemini", "g", 0, T0 + MIN, None)
    rebuild_episodes(db)
    kinds = {(e[1]) for e in episodes(db)}
    assert kinds == {"proj", None}
    unlinked = [e for e in episodes(db) if e[1] is None]
    assert len(unlinked) == 1  # both web sources merge in the unlinked bucket


def test_idempotent_rebuild_same_ids_and_events_untouched(db):
    for i, (p, off) in enumerate([("proj", 0), ("other", 5), ("proj", 300), ("other", 5)]):
        add_event(db, "opencode", f"s{i}", 0, T0 + off * MIN, p)
    before_events = db.execute("SELECT * FROM events ORDER BY id").fetchall()
    rebuild_episodes(db)
    first = episodes(db)
    members = db.execute("SELECT * FROM episode_events ORDER BY 1, 2").fetchall()
    rebuild_episodes(db)
    assert episodes(db) == first
    assert db.execute("SELECT * FROM episode_events ORDER BY 1, 2").fetchall() == members
    assert [tuple(r) for r in db.execute("SELECT * FROM events ORDER BY id")] == [
        tuple(r) for r in before_events
    ]
    # Wipe the derived tables entirely: rebuildable from events alone.
    db.execute("DELETE FROM episode_events")
    db.execute("DELETE FROM episodes")
    rebuild_episodes(db)
    assert episodes(db) == first


def test_empty_db(db):
    assert rebuild_episodes(db) == 0
    assert build_timeline(db, UTC) == []
    assert render_timeline([], UTC) == "no activity"


# ------------------------------------------------------------------ time zones


def test_local_day_bounds_dst():
    # Berlin spring-forward 2026-03-29 is 23 hours; fall-back 2026-10-25 is 25 hours.
    lo, hi = local_day_bounds(date(2026, 3, 29), BERLIN)
    assert (hi + 1 - lo) == 23 * 60 * MIN
    lo, hi = local_day_bounds(date(2026, 10, 25), BERLIN)
    assert (hi + 1 - lo) == 25 * 60 * MIN
    lo, hi = local_day_bounds(date(2026, 10, 26), BERLIN)
    assert (hi + 1 - lo) == 24 * 60 * MIN


def test_parse_day_and_local():
    now = datetime(2026, 10, 28, 1, 30, tzinfo=UTC)  # still Oct 27 in New York
    assert parse_day("today", NY, now=now) == date(2026, 10, 27)
    assert parse_day("yesterday", NY, now=now) == date(2026, 10, 26)
    assert parse_day("today", UTC, now=now) == date(2026, 10, 28)
    assert parse_day("2026-02-03", UTC) == date(2026, 2, 3)
    with pytest.raises(ValueError):
        parse_day("lastweek", UTC)
    assert parse_local("2026-10-27", BERLIN) == ms(2026, 10, 27, 0, 0, BERLIN)
    assert parse_local("2026-10-27", BERLIN, end_of_day=True) == ms(2026, 10, 28, 0, 0, BERLIN) - 1
    assert parse_local("2026-10-27T09:30", BERLIN) == ms(2026, 10, 27, 9, 30, BERLIN)
    assert parse_local("2026-10-27T09:30Z", BERLIN) == ms(2026, 10, 27, 9, 30)
    assert parse_local("7d", BERLIN, now=now) == int((now - timedelta(days=7)).timestamp() * 1000)
    with pytest.raises(ValueError):
        parse_local("garbage", BERLIN)


def test_resolve_tz(monkeypatch):
    assert resolve_tz("UTC") is UTC
    assert resolve_tz("Europe/Berlin").key == "Europe/Berlin"
    with pytest.raises(ValueError, match="unknown time zone"):
        resolve_tz("Mars/Olympus")
    monkeypatch.setenv("CTX_TZ", "Asia/Tokyo")
    assert resolve_tz().key == "Asia/Tokyo"
    assert resolve_tz("UTC") is UTC  # explicit beats env
    monkeypatch.delenv("CTX_TZ")
    assert resolve_tz() is not None


def test_timeline_day_depends_on_tz(db):
    # 23:30 UTC on Oct 27 is already Oct 28 in Berlin and still Oct 27 in New York.
    add_event(db, "opencode", "a", 0, ms(2026, 10, 27, 23, 30), "proj")
    rebuild_episodes(db)
    assert [e.day for e in build_timeline(db, UTC)] == ["2026-10-27"]
    assert [e.day for e in build_timeline(db, BERLIN)] == ["2026-10-28"]
    assert [e.day for e in build_timeline(db, NY)] == ["2026-10-27"]


def test_dst_fall_back_day_filter(db):
    # Berlin 2026-10-25 has 25 hours: 22:30 UTC the 24th is 00:30 local (start of the 25th);
    # 22:59 UTC the 25th is 23:59 local; 23:30 UTC the 25th is 00:30 on the 26th.
    add_event(db, "opencode", "a", 0, ms(2026, 10, 24, 22, 30), "proj")
    add_event(db, "opencode", "b", 0, ms(2026, 10, 25, 22, 59), "other")  # 23:59 local 25th
    add_event(db, "opencode", "c", 0, ms(2026, 10, 25, 23, 30), "proj")  # 00:30 local 26th
    rebuild_episodes(db)
    lo, hi = local_day_bounds(date(2026, 10, 25), BERLIN)
    days = build_timeline(db, BERLIN, since=lo, until=hi)
    assert [(e.day, e.project_id) for e in days] == [
        ("2026-10-25", "proj"),
        ("2026-10-25", "other"),
    ]
    assert build_timeline(db, BERLIN)[0].day == "2026-10-26"  # c lands on the next local day


def test_episode_spanning_midnight_is_split_per_day(db):
    base = ms(2026, 10, 27, 23, 40)  # UTC
    for i in range(4):
        add_event(db, "opencode", "a", i, base + i * 20 * MIN, "proj")  # 23:40..00:40
    rebuild_episodes(db)
    entries = build_timeline(db, UTC)
    assert [(e.day, e.events, e.continues_prev, e.continues_next) for e in entries] == [
        ("2026-10-28", 3, True, False),
        ("2026-10-27", 1, False, True),
    ]
    assert entries[0].episode_id == entries[1].episode_id
    # Newest day first overall.


# ------------------------------------------------------------------ timeline content


def seed_week(db):
    # Tuesday: proj worked on in two sources; other in one; plus a web chat (unlinked).
    add_event(db, "opencode", "a", 0, T0, "proj", title="Fix auth")
    add_event(
        db,
        "opencode",
        "a",
        1,
        T0 + 5 * MIN,
        "proj",
        kind="tool:edit",
        text='input: {"file_path": "/src/auth.py"}',
    )
    add_event(
        db,
        "opencode",
        "a",
        2,
        T0 + 6 * MIN,
        "proj",
        kind="tool:edit",
        text='input: {"file_path": "/src/auth.py"}',
    )
    add_event(
        db,
        "opencode",
        "a",
        3,
        T0 + 7 * MIN,
        "proj",
        kind="tool:bash",
        text='input: {"command": "pytest -q\\nsecond line"}',
    )
    add_event(db, "claude_code", "c", 0, T0 + 30 * MIN, "proj", title="Review auth")
    add_event(
        db,
        "claude_code",
        "c",
        1,
        T0 + 31 * MIN,
        "proj",
        kind="tool:read",
        text='input: {"path": "/src/db.py"}',
    )
    add_event(db, "codex", "x", 0, T0 + 2 * MIN, "other", title="Other work")
    add_event(db, "chatgpt", "w", 0, T0 + 3 * MIN, None, title="Pasta recipe")
    add_event(db, "opencode", "h", 0, T0 + 4 * MIN, "home", title="Private")
    # Wednesday
    add_event(db, "opencode", "d", 0, T0 + 24 * 60 * MIN, "proj", title="Next day")
    rebuild_episodes(db)


def test_timeline_cross_source_summary(db):
    seed_week(db)
    entries = build_timeline(db, UTC)
    assert [e.day for e in entries] == ["2026-10-28", "2026-10-27", "2026-10-27"]
    tue = [e for e in entries if e.day == "2026-10-27"]
    proj = next(e for e in tue if e.project_id == "proj")
    assert proj.sources == ["claude_code", "opencode"]
    assert proj.events == 6 and proj.messages == 2 and proj.tool_calls == 4
    assert {s.title for s in proj.sessions} == {"Fix auth", "Review auth"}
    assert proj.top_files[0] == ("/src/auth.py", 2)
    assert ("/src/db.py", 1) in proj.top_files
    assert proj.top_commands == [("pytest -q", 1)]  # first line only
    assert (proj.start_ts, proj.end_ts) == (T0, T0 + 31 * MIN)
    # Non-allowed and unlinked are hidden by default, shown with include_all.
    shown = {e.project_name for e in tue}
    assert shown == {"Proj", "Other"}
    everything = build_timeline(db, UTC, include_all=True)
    assert {e.project_name for e in everything} >= {"Home", "(unlinked)"}


def test_timeline_filters(db):
    seed_week(db)
    only_cc = build_timeline(db, UTC, source="claude_code")
    assert len(only_cc) == 1 and only_cc[0].sources == ["claude_code"] and only_cc[0].events == 2
    assert [e.project_id for e in build_timeline(db, UTC, project="Other")] == ["other"]
    assert [e.project_id for e in build_timeline(db, UTC, project="proj", since=T0 + 3600_000)] == [
        "proj"
    ]
    assert build_timeline(db, UTC, until=T0 - 1) == []
    assert len(build_timeline(db, UTC, limit=1)) == 1
    assert build_timeline(db, UTC, limit=1)[0].day == "2026-10-28"  # most recent kept
    with pytest.raises(ValueError, match="unknown project"):
        build_timeline(db, UTC, project="nope")


def test_exclude_mcp(db):
    add_event(db, "opencode", "s", 0, T0, "secret")
    rebuild_episodes(db)
    assert len(build_timeline(db, UTC)) == 1
    assert build_timeline(db, UTC, exclude_mcp=True) == []


def test_tool_targets_variants():
    assert tool_targets('input: {"filePath": "/a", "cmd": ["ls", "-l"]}') == (["/a"], ["ls -l"])
    assert tool_targets("input: {truncated json") == ([], [])
    assert tool_targets("plain text") == ([], [])
    assert tool_targets("input: [1, 2]") == ([], [])


def test_secrets_redacted_in_output(db):
    add_event(db, "opencode", "a", 0, T0, "proj", title=f"key {FAKE_TOKEN}")
    add_event(
        db,
        "opencode",
        "a",
        1,
        T0 + MIN,
        "proj",
        kind="tool:bash",
        text=f'input: {{"command": "curl -H {FAKE_TOKEN}"}}',
    )
    rebuild_episodes(db)
    out = render_timeline(build_timeline(db, UTC), UTC)
    assert FAKE_TOKEN not in out and "commands:" in out
    assert FAKE_TOKEN not in json.dumps([e.top_commands for e in build_timeline(db, UTC)])


def test_render_format(db):
    seed_week(db)
    out = render_timeline(build_timeline(db, UTC), UTC)
    assert "2026-10-27 Tue (UTC)" in out and "Proj [09:00-09:31]" in out
    assert "files: /src/auth.py x2" in out and "- opencode: Fix auth" in out


# ------------------------------------------------------------------ projects


def test_list_projects_stats(db):
    seed_week(db)
    by_id = {p["id"]: p for p in list_projects(db)}
    assert by_id["proj"]["events"] == 7 and by_id["proj"]["sessions"] == 3
    assert by_id["proj"]["events_by_source"] == {"claude_code": 2, "opencode": 5}
    assert by_id["proj"]["episodes"] == 2 and by_id["proj"]["allowed"] is True
    assert by_id["home"]["allowed"] is False
    assert by_id["secret"]["events"] == 0 and by_id["secret"]["last_ts"] is None


# ------------------------------------------------------------------ CLI


def cli(*args):
    return runner.invoke(app, list(args))


@pytest.fixture()
def seeded(db):
    seed_week(db)


def test_cli_requires_db():
    r = cli("timeline")
    assert r.exit_code == 1 and "ctx sync" in r.output


def test_cli_timeline_day_and_json(seeded):
    r = cli("timeline", "--day", "2026-10-27", "--tz", "UTC")
    assert r.exit_code == 0, r.output
    assert "Proj" in r.output and "Next day" not in r.output and "Pasta" not in r.output
    data = json.loads(cli("timeline", "--day", "2026-10-27", "--tz", "UTC", "--json").output)
    assert data["timezone"] == "UTC" and len(data["entries"]) == 2
    assert {e["project_id"] for e in data["entries"]} == {"proj", "other"}
    # Same instant, different zone: Berlin sees 10:00 local.
    assert (
        "Proj [10:00-10:31]"
        in cli("timeline", "--day", "2026-10-27", "--tz", "Europe/Berlin").output
    )


def test_cli_timeline_filters_and_errors(seeded):
    assert "Other work" in cli("timeline", "-p", "other", "--tz", "UTC").output
    out = cli("timeline", "-s", "claude_code", "--tz", "UTC").output
    assert "Review auth" in out and "Fix auth" not in out
    out = cli("timeline", "--since", "2026-10-28", "--tz", "UTC").output
    assert "Next day" in out and "Fix auth" not in out
    out = cli("timeline", "--until", "2026-10-27", "--tz", "UTC").output
    assert "Fix auth" in out and "Next day" not in out
    assert "Pasta recipe" in cli("timeline", "--all", "--tz", "UTC").output
    assert "no activity" in cli("timeline", "--day", "2020-01-01", "--tz", "UTC").output
    for args in (
        ["--day", "x"],
        ["--tz", "Nope/Zone"],
        ["-p", "zzz"],
        ["--day", "today", "--since", "1d"],
    ):
        r = cli("timeline", *args)
        assert r.exit_code == 2 and r.output.startswith("error:"), args


def test_cli_timeline_today_yesterday(db):
    now = datetime.now(UTC)
    add_event(db, "opencode", "t", 0, int(now.timestamp() * 1000), "proj", title="Today work")
    add_event(
        db,
        "opencode",
        "y",
        0,
        int((now - timedelta(days=1)).timestamp() * 1000),
        "proj",
        title="Yesterday work",
    )
    rebuild_episodes(db)
    assert "Today work" in cli("timeline", "--day", "today", "--tz", "UTC").output
    out = cli("timeline", "--day", "yesterday", "--tz", "UTC").output
    assert "Yesterday work" in out and "Today work" not in out


def test_cli_reindex_rebuilds_episodes(seeded, db):
    db.execute("DELETE FROM episode_events")
    db.execute("DELETE FROM episodes")
    before = cli("timeline", "--tz", "UTC").output
    assert before.strip() == "no activity"
    r = cli("reindex")
    assert r.exit_code == 0 and "10 events, 5 episodes" in r.output
    assert "Fix auth" in cli("timeline", "--tz", "UTC").output
    r = cli("reindex", "--gap", "1")
    assert r.exit_code == 0 and "episodes" in r.output


def test_cli_project_list_show(seeded):
    out = cli("project", "list", "--tz", "UTC").output
    assert "Proj" in out and "Home" in out and "2026-10-28 09:00" in out
    rows = json.loads(cli("project", "list", "--json").output)
    assert {p["id"] for p in rows} == {"proj", "other", "secret", "home"}
    shown = cli("project", "show", "proj", "--tz", "UTC").output
    assert "allowed:      True" in shown and "opencode=5" in shown and "claude_code=2" in shown
    assert json.loads(cli("project", "show", "Home", "--json").output)["allowed"] is False
    bad = cli("project", "show", "nope")
    assert bad.exit_code == 2 and "unknown project" in bad.output


def test_cli_project_list_empty(tmp_path):
    connect(paths.db_path()).close()
    assert "no projects" in cli("project", "list").output


def test_sync_rebuilds_episodes(tmp_path, monkeypatch):
    from tests.helpers import PROJECTS_TOML, make_opencode_root

    make_opencode_root(tmp_path / "oc")
    monkeypatch.setenv("CTX_SOURCE_ROOT_OPENCODE", str(tmp_path / "oc"))
    (tmp_path / "ctx_home").mkdir(parents=True, exist_ok=True)
    (tmp_path / "ctx_home" / "projects.toml").write_text(PROJECTS_TOML)
    assert cli("sync", "--source", "opencode").exit_code == 0
    conn = connect(paths.db_path())
    first = episodes(conn)
    assert first
    assert cli("sync", "--source", "opencode").exit_code == 0
    assert episodes(conn) == first  # re-sync: same ids, nothing duplicated
    out = cli("timeline", "--tz", "UTC").output
    assert "Sub Project" in out and "Proj" in out


# ------------------------------------------------------------------ MCP


def call(args=None):
    async def go():
        async with Client(build_server()) as client:
            return await client.call_tool("timeline", args or {})

    return asyncio.run(go())


def text_of(result) -> str:
    return "\n".join(c.text for c in result.content)


def test_mcp_timeline(seeded):
    out = text_of(call({"day": "2026-10-27", "tz": "UTC"}))
    assert "Proj [09:00-09:31]" in out and "Fix auth" in out and "Review auth" in out
    assert "Private" not in out and "Pasta" not in out  # not allowed / unlinked
    assert "Next day" not in out
    out = text_of(call({"project": "proj", "source": "claude_code", "tz": "UTC"}))
    assert "Review auth" in out and "Fix auth" not in out
    assert text_of(call({"since": "2030-01-01", "tz": "UTC"})) == "no activity"


def test_mcp_timeline_excluded_and_errors(db):
    add_event(db, "opencode", "s", 0, T0, "secret", title="classified")
    add_event(db, "opencode", "h", 0, T0, "home", title="private")
    rebuild_episodes(db)
    assert text_of(call({"tz": "UTC"})) == "no activity"
    for args in (
        {"project": "secret"},
        {"project": "home"},
        {"tz": "Nope/X"},
        {"day": "bad"},
        {"limit": 0},
        {"source": "nope"},
        {"day": "today", "since": "1d"},
    ):
        r = call(args)
        assert r.is_error, args
        assert "classified" not in text_of(r)


def test_mcp_timeline_limit_cap_and_redaction(db):
    for i in range(60):
        add_event(
            db, "opencode", f"s{i}", 0, T0 + i * 100 * MIN, "proj", title=f"t{i} {FAKE_TOKEN}"
        )
    rebuild_episodes(db)
    out = text_of(call({"tz": "UTC", "limit": 1000}))
    assert FAKE_TOKEN not in out
    assert out.count("episode=") == 50  # MAX_LIMIT
    assert text_of(call({"tz": "UTC"})).count("episode=") == 30
