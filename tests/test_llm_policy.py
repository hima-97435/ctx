"""Rule 9 in code: cloud refusal for chat and embeddings, mixed scopes, unlinked data."""

import pytest
from typer.testing import CliRunner

from ctx.cli.main import app
from ctx.core import paths
from ctx.core.context import build_context
from ctx.core.db import connect
from ctx.core.embed import LocalOnlyViolation, embed_events, embedding_count
from ctx.core.projects import ProjectConfig, set_cloud_policy, sync_projects
from ctx.core.retrieve import retrieve
from ctx.llm.embedders import gemini_embedder, nvidia_embedder
from ctx.llm.grounded import ask
from ctx.llm.policy import CloudRefused, authorize, cloud_scope_refusal
from ctx.llm.providers import LocalProvider
from tests.llm_helpers import SECRET, FakeTransport, ScriptedProvider, ok, status

runner = CliRunner()


def add_events(conn, project, sid, text, n=2, source="opencode"):
    full = f"{source}:{sid}"
    conn.execute(
        "INSERT INTO sessions(id, source, source_session_id, project_id, started_at, title)"
        " VALUES (?, ?, ?, ?, 1000, ?)",
        (full, source, sid, project, f"title {sid}"),
    )
    for i in range(n):
        conn.execute(
            "INSERT INTO events(source, source_id, ts, project_id, session_id, role, kind,"
            " text, token_estimate, content_hash, message_id)"
            " VALUES (?, ?, ?, ?, ?, 'user', 'text', ?, 10, '0', ?)",
            (source, f"{sid}-{i}", 1000 + i, project, full, f"{text} {i}", f"m{i}"),
        )


@pytest.fixture()
def db():
    conn = connect(paths.db_path())
    sync_projects(
        conn,
        [
            ProjectConfig(id="pub", name="Pub", allowed=True, local_only=False),
            ProjectConfig(id="priv", name="Priv", allowed=True, local_only=True),
        ],
    )
    add_events(conn, "pub", "s1", "zebra public plan")
    add_events(conn, "priv", "s2", "zebra private plan")
    add_events(conn, None, "s3", "zebra webchat plan", source="chatgpt")
    yield conn
    conn.close()


def pkg(conn, project=None, **kw):
    return build_context(conn, "zebra", 2000, project, include_all=True, **kw)


def test_refusal_predicate(db):
    assert cloud_scope_refusal(db, ["pub"]) is None
    assert "priv" in cloud_scope_refusal(db, ["priv"])
    assert "priv" in cloud_scope_refusal(db, ["pub", "priv"])  # one local_only poisons the set
    assert cloud_scope_refusal(db, [])  # no scope: refused
    assert cloud_scope_refusal(db, ["ghost"])  # unknown project: refused
    assert cloud_scope_refusal(db, [None])  # unlinked: local by default


def test_cloud_chat_refused_for_local_only_project_and_nothing_is_sent(db):
    p = ScriptedProvider("answer [E1]")
    with pytest.raises(CloudRefused, match="priv"):
        ask(db, p, pkg(db, "priv"), project="priv")
    assert p.calls == []


def test_cloud_chat_allowed_for_opted_in_project(db):
    package = pkg(db, "pub")
    eid = package.items[0].citation.event_id
    p = ScriptedProvider(f"plan [E{eid}]")
    g = ask(db, p, package, project="pub")
    assert g.ok and len(p.calls) == 1
    sent = p.calls[0][1]["content"]
    assert "private" not in sent and "webchat" not in sent


def test_mixed_projects_refuse_cloud_even_if_most_evidence_is_public(db):
    p = ScriptedProvider("x [E1]")
    with pytest.raises(CloudRefused):
        ask(db, p, pkg(db), project=None)  # pub + priv + unlinked all in scope
    assert p.calls == []


def test_unlinked_data_is_local_unless_pseudo_project_opts_in(db):
    package = build_context(db, "webchat", 2000, None, include_all=True)
    assert {i.citation.project_id for i in package.items} == {None}
    p = ScriptedProvider("a [E3]")
    with pytest.raises(CloudRefused):
        ask(db, p, package)
    set_cloud_policy(db, "unlinked", allow_cloud=True)
    eid = package.items[0].citation.event_id
    p = ScriptedProvider(f"a [E{eid}]")
    assert ask(db, p, package).ok
    set_cloud_policy(db, "unlinked", allow_cloud=False)  # and back
    with pytest.raises(CloudRefused):
        ask(db, ScriptedProvider("a"), package)


def test_local_provider_always_allowed_for_local_only(db):
    p = ScriptedProvider("a [E1]", name="ollama", is_cloud=False)
    assert authorize(db, p, ["priv", None])


def test_authorize_is_what_gates_a_real_cloud_provider_object(db, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", SECRET)
    from ctx.llm.providers import GeminiProvider

    t = FakeTransport(ok({"choices": [{"message": {"content": "x"}}]}))
    gp = GeminiProvider(transport=t)
    with pytest.raises(CloudRefused):
        authorize(db, gp, ["priv"])
    with pytest.raises(CloudRefused):
        gp.chat([], clearance=None)
    assert t.requests == []
    assert not LocalProvider().is_cloud


# --- cloud embedders ---


def emb_ok(n, dim=768):
    return ok({"data": [{"index": i, "embedding": [float(i + 1)] * dim} for i in range(n)]})


def test_cloud_embedder_embed_events_only_sends_opted_in_text(db, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", SECRET)

    class Echo(FakeTransport):
        def post(self, url, headers, body, timeout):
            import json

            n = len(json.loads(body)["input"])
            self.queue = [emb_ok(n)]
            return super().post(url, headers, body, timeout)

    t = Echo(emb_ok(1))
    e = gemini_embedder(transport=t)
    stats = embed_events(db, e)
    sent = [x for r in t.requests for x in r["json"]["input"]]
    assert sent and all("public" in x for x in sent)  # no private / webchat text left
    assert stats.events_skipped_local_only == 4
    assert embedding_count(db, e.name) == 2
    assert t.requests[0]["headers"]["Authorization"] == f"Bearer {SECRET}"
    assert t.requests[0]["json"]["dimensions"] == 768

    set_cloud_policy(db, "unlinked", allow_cloud=True)  # webchat now opted in
    t.requests.clear()
    embed_events(db, e)
    sent = [x for r in t.requests for x in r["json"]["input"]]
    assert sent and all("webchat" in x for x in sent)


def test_cloud_query_embedding_refused_for_local_only_and_unscoped(db):
    t = FakeTransport(emb_ok(1))
    e = nvidia_embedder(transport=t)
    db.execute(  # so retrieval gets as far as the gate
        "INSERT INTO embeddings VALUES (1, 0, ?, 1024, ?, 'h')", (e.name, bytes(4 * 1024))
    )
    for project in ("priv", None):
        with pytest.raises(LocalOnlyViolation):
            retrieve(db, "zebra", mode="vector", embedder=e, project=project, include_all=True)
    assert t.requests == []


def test_cloud_embedder_missing_key_message_has_no_values(db, monkeypatch):
    for v in ("CTX_NVIDIA_API_KEY", "NVIDIA_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    from ctx.core.embed import EmbedderUnavailable

    e = nvidia_embedder(transport=FakeTransport(emb_ok(1, 1024)))
    with pytest.raises(EmbedderUnavailable, match="CTX_NVIDIA_API_KEY"):
        e.embed(["x"])


def test_cloud_embedder_server_error_does_not_leak_key(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", SECRET)
    monkeypatch.delenv("CTX_NVIDIA_API_KEY", raising=False)
    from ctx.core.embed import EmbedderUnavailable

    e = nvidia_embedder(transport=FakeTransport(status(401, f"bad {SECRET}")))
    with pytest.raises(EmbedderUnavailable) as exc:
        e.embed(["x"])
    assert SECRET not in str(exc.value)


def test_embedder_specs_resolve():
    from ctx.core.embed import get_embedder

    assert get_embedder("gemini:").name == "gemini:gemini-embedding-001"
    assert get_embedder("nvidia:nvidia/nv-embedqa-e5-v5").dim == 1024
    assert get_embedder("gemini:").is_cloud


# --- project set ---


def test_project_set_cli(db):
    r = runner.invoke(app, ["project", "set", "priv", "--allow-cloud"])
    assert r.exit_code == 0 and "local_only=0" in r.output
    assert db.execute("SELECT local_only FROM projects WHERE id='priv'").fetchone()[0] == 0
    r = runner.invoke(app, ["project", "set", "Priv", "--local-only"])
    assert r.exit_code == 0
    assert db.execute("SELECT local_only FROM projects WHERE id='priv'").fetchone()[0] == 1
    assert runner.invoke(app, ["project", "set", "priv"]).exit_code == 2
    assert (
        runner.invoke(app, ["project", "set", "priv", "--local-only", "--allow-cloud"]).exit_code
        == 2
    )
    assert runner.invoke(app, ["project", "set", "nope", "--allow-cloud"]).exit_code == 2
    assert runner.invoke(app, ["project", "set", "unlinked", "--allow-cloud"]).exit_code == 0
    assert db.execute("SELECT local_only FROM projects WHERE id='unlinked'").fetchone()[0] == 0
