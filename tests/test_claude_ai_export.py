import pytest

from ctx.connectors.base import ConnectorError
from ctx.connectors.claude_ai_export import ClaudeAiExportConnector
from ctx.core.blobs import BlobStore
from ctx.core.db import connect
from ctx.core.model import RawEvent, RawSession
from ctx.core.sync import sync
from tests.connector_contract import ConnectorContract
from tests.fixtures.web_exports import (
    CANARY,
    FAKE_TOKEN,
    claude_conversation,
    claude_zip,
    make_zip,
)


class TestClaudeAiContract(ConnectorContract):
    expects_sql = False

    @pytest.fixture()
    def connector(self, tmp_path):
        return ClaudeAiExportConnector(source_root=tmp_path / "imports" / "claude_ai")

    @pytest.fixture()
    def source_path(self, connector):
        claude_zip(connector.source_root / "data.zip")
        return connector.source_root

    @pytest.fixture()
    def bad_source_path(self, connector):
        make_zip(connector.source_root / "bad.zip", {"conversations.json": [{"uuid": "x"}]})
        return connector.source_root

    @pytest.fixture()
    def secret_canaries(self):
        return ["abcdef123456xyz", FAKE_TOKEN]

    @pytest.fixture()
    def denylisted_canaries(self):
        return [CANARY]


def fetch(c, path, tmp_path):
    return list(c.fetch(path, blob_store=BlobStore(tmp_path / "b"), tmp_dir=tmp_path / "t"))


@pytest.fixture()
def cc(tmp_path):
    return ClaudeAiExportConnector(source_root=tmp_path / "imp")


def test_mapping(cc, tmp_path):
    z = claude_zip(tmp_path / "imp" / "d.zip")
    records = fetch(cc, z, tmp_path)
    s = next(r for r in records if isinstance(r, RawSession))
    assert s.source_session_id == "cl-1" and s.title == "Doc summary"
    assert s.agent == "project:Research" and s.cwd is None
    ev = {e.source_id: e for e in records if isinstance(e, RawEvent)}
    assert ev["cl-1:m1:0"].role == "user" and ev["cl-1:m1:att0"].kind == "file"
    assert (
        "doc body" in ev["cl-1:m1:att0"].text and "abcdef123456xyz" not in ev["cl-1:m1:att0"].text
    )
    assert ev["cl-1:m1:file0"].text == "[file: pic.png]"
    assert ev["cl-1:m2:0"].kind == "reasoning"
    assert ev["cl-1:m2:2"].kind == "tool:web_search"
    assert ev["cl-1:m2:3"].kind == "tool_result" and ev["cl-1:m2:3"].text == "result text"
    assert ev["cl-1:m3:0"].text == "plain text only"  # text fallback when content is empty
    assert not any(k.startswith("cl-1:m2:4") for k in ev)
    assert cc.last_stats.skipped["unknown_block:flux_capacitor"] == 1


def test_unknown_sender_and_malformed(cc, tmp_path):
    conv = claude_conversation()
    conv["chat_messages"] += [{"uuid": "z", "sender": "robot", "text": "x"}, "junk"]
    z = claude_zip(tmp_path / "imp" / "d.zip", [conv, "junk", {"uuid": "", "chat_messages": []}])
    fetch(cc, z, tmp_path)
    st = cc.last_stats.skipped
    assert st["unknown_sender:robot"] == 1 and st["malformed_message"] == 1
    assert st["malformed_conversation"] == 1 and st["conversation_without_id"] == 1


def test_projects_json_optional_or_malformed(cc, tmp_path):
    z = claude_zip(tmp_path / "imp" / "a.zip", projects=False)
    s = next(r for r in fetch(cc, z, tmp_path) if isinstance(r, RawSession))
    assert s.agent is None
    z2 = claude_zip(tmp_path / "imp" / "b.zip", extra={"projects.json": "{bad"})
    s2 = next(r for r in fetch(cc, z2, tmp_path) if isinstance(r, RawSession))
    assert s2.agent is None and any("projects.json" in w for w in cc.last_stats.warnings)


def test_users_json_never_read(cc, tmp_path, monkeypatch):
    import zipfile

    opened = []
    real = zipfile.ZipFile.read
    monkeypatch.setattr(
        zipfile.ZipFile,
        "read",
        lambda self, n, *a, **k: (opened.append(getattr(n, "filename", n)), real(self, n, *a, **k))[
            1
        ],
    )
    fetch(cc, claude_zip(tmp_path / "imp" / "d.zip"), tmp_path)
    assert "users.json" not in opened


def test_invalid_json_and_missing_conversations(cc, tmp_path):
    bad = make_zip(tmp_path / "imp" / "bad.zip", {"conversations.json": "[1,"})
    with pytest.raises(ConnectorError, match="invalid JSON"):
        cc.check_version(bad)
    only_projects = make_zip(tmp_path / "imp" / "p.zip", {"projects.json": []})
    with pytest.raises(ConnectorError, match="not a claude_ai export"):
        cc.check_version(only_projects)


def test_idempotent_superset(tmp_path):
    conn = connect(tmp_path / "db.sqlite")
    c = ClaudeAiExportConnector(source_root=tmp_path / "imp")
    kw = {"source_paths": {"claude_ai": tmp_path / "imp"}, "data_dir": tmp_path / "data"}
    claude_zip(tmp_path / "imp" / "1.zip")
    first = sync(conn, {"claude_ai": c}, **kw)[0]
    assert first.status == "ok" and first.events_new == 8
    assert sync(conn, {"claude_ai": c}, **kw)[0].events_new == 0
    claude_zip(tmp_path / "imp" / "2.zip", [claude_conversation(extra_message=True)])
    third = sync(conn, {"claude_ai": c}, **kw)[0]
    assert third.events_new == 1 and third.sessions_new == 0
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 9
