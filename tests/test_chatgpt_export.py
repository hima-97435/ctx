import pytest

from ctx.connectors.base import ConnectorError, DenylistViolation
from ctx.connectors.chatgpt_export import ChatGPTExportConnector
from ctx.core.blobs import BlobStore
from ctx.core.db import connect
from ctx.core.model import RawEvent, RawSession
from ctx.core.search import search
from ctx.core.sync import sync
from tests.connector_contract import ConnectorContract
from tests.fixtures.web_exports import (
    CANARY,
    FAKE_TOKEN,
    chatgpt_zip,
    gpt_conversation,
    make_zip,
)


class TestChatGPTContract(ConnectorContract):
    expects_sql = False

    @pytest.fixture()
    def connector(self, tmp_path):
        return ChatGPTExportConnector(source_root=tmp_path / "imports" / "chatgpt")

    @pytest.fixture()
    def source_path(self, connector):
        chatgpt_zip(connector.source_root / "export.zip")
        return connector.source_root

    @pytest.fixture()
    def bad_source_path(self, connector):
        make_zip(connector.source_root / "bad.zip", {"conversations.json": [{"id": "x", "foo": 1}]})
        return connector.source_root

    @pytest.fixture()
    def secret_canaries(self):
        return ["abcdef123456xyz", FAKE_TOKEN]

    @pytest.fixture()
    def denylisted_canaries(self):
        return [CANARY, "ALTERNATE-BRANCH-TEXT"]


def fetch(connector, path, tmp_path, state=None):
    store = BlobStore(tmp_path / "blobs_x")
    return list(connector.fetch(path, blob_store=store, tmp_dir=tmp_path / "t", state=state))


@pytest.fixture()
def conn_root(tmp_path):
    return ChatGPTExportConnector(source_root=tmp_path / "imp")


def events_of(records):
    return [r for r in records if isinstance(r, RawEvent)]


def test_canonical_branch_mapping(conn_root, tmp_path):
    z = chatgpt_zip(tmp_path / "imp" / "e.zip")
    records = fetch(conn_root, z.parent, tmp_path)
    sessions = [r for r in records if isinstance(r, RawSession)]
    assert len(sessions) == 1 and sessions[0].source_session_id == "conv-1"
    assert sessions[0].title == "Plan the migration" and sessions[0].model == "gpt-4o"
    assert sessions[0].cwd is None
    ev = {e.message_ref: e for e in events_of(records)}
    assert list(ev) == [
        "u1",
        "a1",
        "u2",
        "c1",
        "o1",
        "b1",
        "q1",
        "a2",
    ]  # hidden sys, alt, unknown skipped
    assert ev["u1"].role == "user" and ev["u1"].source_id == "conv-1:u1"
    assert ev["u2"].text.startswith("[image: 640x480]") and "screenshot" in ev["u2"].text
    assert ev["c1"].kind == "tool:python" and "print('hi')" in ev["c1"].text
    assert ev["o1"].kind == "tool_result" and ev["o1"].role is None
    assert ev["b1"].kind == "tool_result" and "Search result page" in ev["b1"].text
    assert "https://example.com" in ev["q1"].text
    st = conn_root.last_stats.skipped
    assert st["alternate_branch_message"] == 1
    assert st["hidden_message"] == 1
    assert st["unknown_content_type:weird_new_type"] == 1


def test_secrets_redacted(conn_root, tmp_path):
    z = chatgpt_zip(tmp_path / "imp" / "e.zip")
    text = "\n".join(e.text for e in events_of(fetch(conn_root, z, tmp_path)))
    assert "abcdef123456xyz" not in text and FAKE_TOKEN not in text and "[REDACTED" in text


def test_sharded_files_and_cross_shard_dedupe(conn_root, tmp_path):
    c1, c2 = gpt_conversation("c-a", "A"), gpt_conversation("c-b", "B")
    z = chatgpt_zip(tmp_path / "imp" / "e.zip", [c1, c2, c1], sharded=True)
    records = fetch(conn_root, z, tmp_path)
    assert sorted(r.source_session_id for r in records if isinstance(r, RawSession)) == [
        "c-a",
        "c-b",
    ]
    assert len({e.source_id for e in events_of(records)}) == len(events_of(records)) == 16
    assert conn_root.last_stats.skipped["duplicate_in_run"] == 8


def test_extracted_directory(conn_root, tmp_path):
    import json

    d = tmp_path / "imp" / "extracted"
    d.mkdir(parents=True)
    (d / "conversations.json").write_text(json.dumps([gpt_conversation()]))
    (d / "user.json").write_text(CANARY)
    records = fetch(conn_root, d.parent, tmp_path)
    assert len(events_of(records)) == 8
    assert CANARY not in repr(records)


def test_no_current_node_uses_newest_leaf(conn_root, tmp_path):
    conv = gpt_conversation()
    conv["current_node"] = None
    z = chatgpt_zip(tmp_path / "imp" / "e.zip", [conv])
    refs = [e.message_ref for e in events_of(fetch(conn_root, z, tmp_path))]
    assert refs[-1] == "a2" and conn_root.last_stats.skipped["no_current_node"] == 1


def test_cyclic_parent_chain_terminates(conn_root, tmp_path):
    conv = gpt_conversation()
    conv["mapping"]["u1"]["parent"] = "a2"  # cycle
    z = chatgpt_zip(tmp_path / "imp" / "e.zip", [conv])
    assert fetch(conn_root, z, tmp_path)  # no hang


def test_malformed_and_skipped_conversations(conn_root, tmp_path):
    good = gpt_conversation("ok")
    odd = [good, "not a dict", {"id": "no-mapping"}, {"mapping": {}, "title": "no id"}]
    z = chatgpt_zip(tmp_path / "imp" / "e.zip", odd)
    fetch(conn_root, z, tmp_path)
    st = conn_root.last_stats.skipped
    assert st["malformed_conversation"] == 2 and st["conversation_without_id"] == 1


def test_personal_context_not_imported(conn_root, tmp_path):
    conv = gpt_conversation()
    conv["mapping"]["u1"]["message"]["content"] = {
        "content_type": "user_editable_context",
        "user_profile": "I live at SECRET-ADDRESS",
    }
    z = chatgpt_zip(tmp_path / "imp" / "e.zip", [conv])
    records = fetch(conn_root, z, tmp_path)
    assert "SECRET-ADDRESS" not in repr(records)
    assert conn_root.last_stats.skipped["personal_context:user_editable_context"] == 1


def test_invalid_json_fails_loud(conn_root, tmp_path):
    make_zip(tmp_path / "imp" / "bad.zip", {"conversations.json": "{not json"})
    with pytest.raises(ConnectorError, match="invalid JSON"):
        conn_root.check_version(tmp_path / "imp")
    assert (
        sync(
            connect(tmp_path / "x.db"),
            {"chatgpt": conn_root},
            source_paths={"chatgpt": tmp_path / "imp"},
            data_dir=tmp_path / "d",
        )[0].status
        == "error"
    )


def test_wrong_shape_top_level(conn_root, tmp_path):
    z = make_zip(tmp_path / "imp" / "bad.zip", {"conversations.json": {"a": 1}})
    with pytest.raises(ConnectorError, match="list"):
        conn_root.check_version(z)


def test_missing_files_and_foreign_zip(conn_root, tmp_path):
    z = make_zip(tmp_path / "imp" / "other.zip", {"readme.txt": "hi"})
    with pytest.raises(ConnectorError, match="not a chatgpt export"):
        conn_root.check_version(z)
    # in a watch dir a foreign zip is ignored with a warning, not an error
    chatgpt_zip(tmp_path / "imp" / "good.zip")
    fetch(conn_root, tmp_path / "imp", tmp_path)
    assert any("other.zip" in w for w in conn_root.last_stats.warnings)
    with pytest.raises(ConnectorError):
        conn_root.check_version(tmp_path / "imp" / "missing.zip")
    notzip = tmp_path / "imp" / "broken.zip"
    notzip.write_bytes(b"this is not a zip")
    with pytest.raises(ConnectorError, match="not a readable zip"):
        conn_root.check_version(notzip)
    with pytest.raises(ConnectorError, match="expected a .zip"):
        (tmp_path / "imp" / "a.txt").write_text("x")
        conn_root.check_version(tmp_path / "imp" / "a.txt")


def test_empty_watch_dir(conn_root, tmp_path):
    (tmp_path / "imp").mkdir()
    assert conn_root.check_version(tmp_path / "imp").endswith(":empty")
    assert fetch(conn_root, tmp_path / "imp", tmp_path) == []


def test_denylisted_members_never_opened(conn_root, tmp_path, monkeypatch):
    import zipfile

    opened: list[str] = []
    real = zipfile.ZipFile.read

    def spy(self, name, *a, **k):
        opened.append(getattr(name, "filename", name))
        return real(self, name, *a, **k)

    monkeypatch.setattr(zipfile.ZipFile, "read", spy)
    z = chatgpt_zip(tmp_path / "imp" / "e.zip", extra={"account_auth.json": {"k": CANARY}})
    fetch(conn_root, z, tmp_path)
    assert opened and set(opened) == {"conversations.json"}
    from ctx.connectors.webexport_common import ExportArchive

    with pytest.raises(DenylistViolation):
        ExportArchive(z).read("user.json", conn_root.denylist_paths)


def test_large_message_spills_to_blob(conn_root, tmp_path):
    conv = gpt_conversation()
    conv["mapping"]["a1"]["message"]["content"]["parts"] = ["x" * 200_000]
    z = chatgpt_zip(tmp_path / "imp" / "e.zip", [conv])
    big = [e for e in events_of(fetch(conn_root, z, tmp_path)) if e.message_ref == "a1"][0]
    assert big.blob_ref and big.truncated


def test_idempotent_and_superset_reimport(tmp_path):
    conn = connect(tmp_path / "db.sqlite")
    c = ChatGPTExportConnector(source_root=tmp_path / "imp")
    kw = {"source_paths": {"chatgpt": tmp_path / "imp"}, "data_dir": tmp_path / "data"}
    chatgpt_zip(tmp_path / "imp" / "old.zip", [gpt_conversation()])
    first = sync(conn, {"chatgpt": c}, **kw)[0]
    assert first.status == "ok" and first.events_new == 8 and first.sessions_new == 1
    again = sync(conn, {"chatgpt": c}, **kw)[0]
    assert again.events_new == 0 and again.unchanged_units == 1
    # newer export: same conversation continued + a new conversation; old export still present
    chatgpt_zip(
        tmp_path / "imp" / "new.zip",
        [gpt_conversation(extra_tail=True), gpt_conversation("conv-2", "Other")],
    )
    third = sync(conn, {"chatgpt": c}, **kw)[0]
    assert third.events_new == 1 + 8 and third.sessions_new == 1
    full = sync(conn, {"chatgpt": c}, full=True, **kw)[0]
    assert full.events_new == 0
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 17
    assert (
        conn.execute("SELECT project_id FROM sessions WHERE id='chatgpt:conv-1'").fetchone()[0]
        is None
    )
    assert search(conn, "alembic", include_all=True)
    assert search(conn, "ALTERNATE", include_all=True) == []
