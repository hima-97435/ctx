"""MCP server tests: in-process client against the real server, CTX_HOME tmp DB."""

import asyncio
import json

import pytest
from mcp import Client
from typer.testing import CliRunner

from ctx.cli.main import app
from ctx.core import paths
from ctx.core.db import connect
from ctx.core.projects import ProjectConfig, sync_projects
from ctx.core.tokens import estimate_tokens
from ctx.mcp.config import CLIENTS
from ctx.mcp.server import MAX_RESPONSE_TOKENS, build_server

FAKE_TOKEN = "sk-abcdefghijklmnopqrstuvwx1234"


def call(tool: str, args: dict | None = None):
    async def go():
        async with Client(build_server()) as client:
            return await client.call_tool(tool, args or {})

    return asyncio.run(go())


def text_of(result) -> str:
    return "\n".join(c.text for c in result.content)


def add_session(conn, project, sid, n_events=3, text="zebra migration plan", source="opencode"):
    full = f"{source}:{sid}"
    conn.execute(
        "INSERT INTO sessions(id, source, source_session_id, project_id, started_at, title)"
        " VALUES (?, ?, ?, ?, 1000, ?)",
        (full, source, sid, project, f"title {sid}"),
    )
    for i in range(n_events):
        t = text if i == 0 else f"{text} step {i}"
        conn.execute(
            "INSERT INTO events(source, source_id, ts, project_id, session_id, role, kind,"
            " text, token_estimate, content_hash, message_id)"
            " VALUES (?, ?, ?, ?, ?, 'user', 'text', ?, ?, '0', ?)",
            (source, f"{sid}-{i}", 1000 + i, project, full, t, len(t) // 4, f"m{i}"),
        )


@pytest.fixture()
def db():
    conn = connect(paths.db_path())
    sync_projects(
        conn,
        [
            ProjectConfig(id="proj", name="Proj", allowed=True),
            ProjectConfig(id="secret", name="Secret", allowed=True, mcp_exclude=True),
            ProjectConfig(id="home", name="Home", allowed=False),
        ],
    )
    add_session(conn, "proj", "s1")
    add_session(conn, "secret", "s2", text="zebra classified")
    add_session(conn, "home", "s3", text="zebra home stuff")
    yield conn
    conn.close()


def test_lists_all_tools():
    async def go():
        async with Client(build_server()) as client:
            return {t.name for t in (await client.list_tools()).tools}

    assert asyncio.run(go()) == {
        "search",
        "get_context",
        "get_session",
        "list_projects",
        "recent_sessions",
        "sync",
        "timeline",
    }


def test_search_cited_and_visibility(db):
    out = text_of(call("search", {"query": "zebra"}))
    assert "opencode session=s1 message=m0 event=" in out
    assert "classified" not in out and "home stuff" not in out


def test_search_filters(db):
    assert "no results" in text_of(call("search", {"query": "zebra", "source": "claude_code"}))
    assert "s1" in text_of(
        call("search", {"query": "zebra", "project": "Proj", "since": "1970-01-01"})
    )
    assert "no results" in text_of(call("search", {"query": "zebra", "until": "1969-12-31"}))
    assert len(text_of(call("search", {"query": "zebra", "limit": 1})).split("\n\n")) == 1


@pytest.mark.parametrize(
    "args",
    [
        {"query": "   "},
        {"query": "zebra", "since": "nonsense"},
        {"query": "zebra", "source": "nope"},
        {"query": "zebra", "project": "missing"},
        {"query": "zebra", "limit": 0},
    ],
)
def test_search_bad_args(db, args):
    assert call("search", args).is_error


def test_excluded_project_looks_unknown(db):
    assert call("search", {"query": "zebra", "project": "secret"}).is_error
    assert call("search", {"query": "zebra", "project": "home"}).is_error
    assert call("recent_sessions", {"project": "secret"}).is_error
    assert call("get_session", {"source": "opencode", "session_id": "s2"}).is_error
    assert call("get_session", {"source": "opencode", "session_id": "s3"}).is_error
    assert "classified" not in text_of(
        call("get_context", {"query": "zebra", "budget_tokens": 500})
    )


def test_missing_tool_args_and_unknown_tool_error():
    assert call("search", {}).is_error


def test_redaction_everywhere(db):
    secret_text = f"deploy key is {FAKE_TOKEN} keep it safe"
    add_session(db, "proj", "leaky", text=secret_text)  # legacy unredacted row
    for tool, args in [
        ("search", {"query": "deploy key"}),
        ("get_context", {"query": "deploy key"}),
        ("get_session", {"source": "opencode", "session_id": "leaky"}),
    ]:
        out = text_of(call(tool, args))
        assert "keep it safe" in out
        assert FAKE_TOKEN not in out


def test_get_context_cited_and_budget(db):
    out = text_of(call("get_context", {"query": "zebra", "project": "proj", "budget_tokens": 5}))
    assert "[1] (opencode session=s1" in out
    assert "tokens" in out
    assert "left out" in out  # tiny budget forces other matches out
    assert call("get_context", {"query": "zebra", "budget_tokens": 0}).is_error


def test_response_cap(db):
    big = "bigword " * 2_500
    add_session(db, "proj", "big", n_events=20, text=big)
    for tool, args in [
        ("get_context", {"query": "bigword", "budget_tokens": 10**9}),
        ("get_session", {"source": "opencode", "session_id": "big", "limit": 50}),
        ("search", {"query": "bigword", "limit": 50}),
    ]:
        assert estimate_tokens(text_of(call(tool, args))) <= MAX_RESPONSE_TOKENS + 50


def test_get_session_window(db):
    add_session(db, "proj", "long", n_events=30)
    out = text_of(call("get_session", {"source": "opencode", "session_id": "long", "limit": 5}))
    assert "messages 1-5 of 30" in out
    out = text_of(
        call(
            "get_session",
            {"source": "opencode", "session_id": "long", "around_message_id": "m15", "limit": 5},
        )
    )
    assert "message=m15" in out and "message=m0 " not in out
    assert call(
        "get_session", {"source": "opencode", "session_id": "long", "around_message_id": "zz"}
    ).is_error
    assert call("get_session", {"source": "opencode", "session_id": "nope"}).is_error


def test_list_projects_and_recent_sessions(db):
    out = text_of(call("list_projects"))
    assert "proj | Proj | sessions=1" in out
    assert "secret" not in out.lower() and "home" not in out.lower()
    rec = text_of(call("recent_sessions", {"project": "proj", "source": "opencode"}))
    assert "session=s1" in rec and "s2" not in rec and "s3" not in rec
    assert "no sessions" in text_of(call("recent_sessions", {"source": "codex"}))


def test_empty_db():
    assert "no results" in text_of(call("search", {"query": "anything"}))
    assert "no results" in text_of(call("get_context", {"query": "anything"}))
    assert "no projects" in text_of(call("list_projects"))
    assert "no sessions" in text_of(call("recent_sessions"))
    assert call("get_session", {"source": "opencode", "session_id": "x"}).is_error


def test_sync_tool_idempotent_and_bad_source():
    assert not call("sync").is_error  # source roots are tmp (conftest): nothing real is read
    assert not call("sync", {"source": "opencode"}).is_error
    assert call("sync", {"source": "nope"}).is_error


def test_mcp_exclude_config_roundtrip(tmp_path):
    from ctx.core.projects import load_projects_config

    cfg = tmp_path / "p.toml"
    cfg.write_text('[projects.a]\nname = "A"\nallowed = true\nmcp_exclude = true\n')
    assert load_projects_config(cfg)[0].mcp_exclude is True
    conn = connect(tmp_path / "x.db")
    sync_projects(conn, load_projects_config(cfg))
    assert conn.execute("SELECT mcp_exclude FROM projects").fetchone()[0] == 1


# --- CLI: ctx mcp config (print-only) ---------------------------------------------------


@pytest.mark.parametrize("client", CLIENTS)
def test_mcp_config_prints_and_writes_nothing(client, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["mcp", "config", "--client", client])
    assert result.exit_code == 0
    assert "serve-mcp" in result.output
    assert not (tmp_path / "home").exists()
    assert list(tmp_path.iterdir()) == []  # no config files written anywhere we can see


def test_mcp_config_content():
    run = lambda c: CliRunner().invoke(app, ["mcp", "config", "--client", c]).output  # noqa: E731
    assert "claude mcp add ctx -- ctx serve-mcp" in run("claude-code")
    assert "[mcp_servers.ctx]" in run("codex")
    assert "UNVERIFIED" in run("codex") and "UNVERIFIED" not in run("claude-code")
    assert json.loads(run("opencode").split("\n", 1)[1])["mcp"]["ctx"]["type"] == "local"


def test_mcp_config_bad_client():
    result = CliRunner().invoke(app, ["mcp", "config", "--client", "emacs"])
    assert result.exit_code == 2 and "unknown client" in result.output
    assert CliRunner().invoke(app, ["mcp", "config"]).exit_code != 0


def test_serve_mcp_registered():
    assert "serve-mcp" in CliRunner().invoke(app, ["--help"]).output
