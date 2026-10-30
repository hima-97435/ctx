import pytest

from ctx.connectors.base import ConnectorError
from ctx.connectors.gemini_takeout import SESSION_GAP_MS, GeminiTakeoutConnector, html_to_text
from ctx.core.blobs import BlobStore
from ctx.core.db import connect
from ctx.core.model import RawEvent, RawSession
from ctx.core.sync import sync
from tests.connector_contract import ConnectorContract
from tests.fixtures.web_exports import (
    CANARY,
    GEMINI_JSON_PATH,
    SECRET_LINE,
    gemini_entries,
    gemini_entry,
    gemini_html,
    gemini_zip,
    make_zip,
)


class TestGeminiContract(ConnectorContract):
    expects_sql = False

    @pytest.fixture()
    def connector(self, tmp_path):
        return GeminiTakeoutConnector(source_root=tmp_path / "imports" / "gemini")

    @pytest.fixture()
    def source_path(self, connector):
        gemini_zip(connector.source_root / "takeout-1.zip")
        return connector.source_root

    @pytest.fixture()
    def bad_source_path(self, connector):
        make_zip(connector.source_root / "bad.zip", {GEMINI_JSON_PATH: [{"nope": 1}]})
        return connector.source_root

    @pytest.fixture()
    def secret_canaries(self):
        return ["abcdef123456xyz"]

    @pytest.fixture()
    def denylisted_canaries(self):
        return [CANARY]


def fetch(c, path, tmp_path):
    return list(c.fetch(path, blob_store=BlobStore(tmp_path / "b"), tmp_dir=tmp_path / "t"))


@pytest.fixture()
def gc(tmp_path):
    return GeminiTakeoutConnector(source_root=tmp_path / "imp")


def test_json_sessions_by_gap(gc, tmp_path):
    records = fetch(gc, gemini_zip(tmp_path / "imp" / "t.zip"), tmp_path)
    sessions = [r for r in records if isinstance(r, RawSession)]
    # 10:00 and 10:20 (gap 20min) share a session; 12:00 starts another
    assert len(sessions) == 2 and sessions[0].title == "first question"
    assert sessions[0].ended_at - sessions[0].started_at == 20 * 60 * 1000 < SESSION_GAP_MS
    ev = [r for r in records if isinstance(r, RawEvent)]
    by_session = {
        s.source_session_id: [e for e in ev if e.session_ref == s.source_session_id]
        for s in sessions
    }
    assert sorted(len(v) for v in by_session.values()) == [2, 4]
    texts = "\n".join(e.text for e in ev)
    assert "Answer two" in texts and "- a" in texts and "Searched" not in texts
    assert "abcdef123456xyz" not in texts and "first question" in texts
    st = gc.last_stats.skipped
    assert st["non_prompt_activity"] == 1 and st["non_gemini_activity"] == 1
    assert [e.role for e in ev[:2]] == ["user", "assistant"]


def test_other_activity_products_never_read(gc, tmp_path, monkeypatch):
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
    records = fetch(gc, gemini_zip(tmp_path / "imp" / "t.zip"), tmp_path)
    assert set(opened) == {GEMINI_JSON_PATH} and CANARY not in repr(records)


def test_html_fallback(gc, tmp_path):
    html = gemini_html(
        [
            ("hello there", "Oct 1, 2026, 10:00:00 AM UTC", "<p>Hi <i>you</i></p>"),
            ("later", "Oct 1, 2026, 3:30:00 PM GMT+05:30", "<p>ok</p>"),
            ("tz", "Oct 1, 2026, 4:00:00 PM CEST", ""),
        ]
    )
    z = gemini_zip(tmp_path / "imp" / "t.zip", html=html)
    records = fetch(gc, z, tmp_path)
    ev = [r for r in records if isinstance(r, RawEvent)]
    assert [e.text for e in ev] == ["hello there", "Hi you", "later", "ok", "tz"]
    # 15:30 +05:30 == 10:00 UTC
    assert ev[2].ts == ev[0].ts
    assert any("CEST" in w for w in gc.last_stats.warnings)


def test_json_preferred_over_html(gc, tmp_path):
    z = gemini_zip(
        tmp_path / "imp" / "t.zip",
        extra={
            "Takeout/My Activity/Gemini Apps/MyActivity.html": gemini_html(
                [("html only", "Oct 1, 2026, 1:00:00 AM UTC", "")]
            )
        },
    )
    texts = " ".join(e.text for e in fetch(gc, z, tmp_path) if isinstance(e, RawEvent))
    assert "html only" not in texts and "first question" in texts


def test_html_non_prompt_and_empty(gc, tmp_path):
    html = (
        '<div class="content-cell mdl-cell mdl-cell--6-col mdl-typography--body-1">'
        "Used Gemini Apps<br>Oct 1, 2026, 1:00:00 AM UTC</div>"
    )
    z = gemini_zip(tmp_path / "imp" / "t.zip", html=html)
    assert [r for r in fetch(gc, z, tmp_path) if isinstance(r, RawEvent)] == []
    assert gc.last_stats.skipped["non_prompt_activity"] == 1
    with pytest.raises(ConnectorError, match="HTML not recognized"):
        gc.check_version(gemini_zip(tmp_path / "imp" / "x.zip", html="<html>nothing</html>"))


def test_failures(gc, tmp_path):
    with pytest.raises(ConnectorError, match="invalid JSON"):
        gc.check_version(make_zip(tmp_path / "imp" / "a.zip", {GEMINI_JSON_PATH: "{oops"}))
    with pytest.raises(ConnectorError, match="list"):
        gc.check_version(make_zip(tmp_path / "imp" / "b.zip", {GEMINI_JSON_PATH: {"a": 1}}))
    only_search = make_zip(
        tmp_path / "imp" / "c.zip", {"Takeout/My Activity/Search/MyActivity.json": []}
    )
    with pytest.raises(ConnectorError, match="not a gemini export"):
        gc.check_version(only_search)


def test_extracted_dir_and_malformed_entries(gc, tmp_path):
    import json

    d = tmp_path / "imp" / "Takeout" / "My Activity" / "Gemini Apps"
    d.mkdir(parents=True)
    entries = gemini_entries() + ["junk", {"title": "Prompted x", "time": "garbage"}]
    (d / "MyActivity.json").write_text(json.dumps(entries))
    records = fetch(gc, tmp_path / "imp", tmp_path)
    assert len([r for r in records if isinstance(r, RawSession)]) == 2
    assert gc.last_stats.skipped["malformed_activity"] == 2


def test_html_to_text():
    assert html_to_text("<p>a</p><p>b&amp;c</p><script>x()</script>") == "a\n\nb&c"


def test_idempotent_superset_and_merge(tmp_path):
    conn = connect(tmp_path / "db.sqlite")
    c = GeminiTakeoutConnector(source_root=tmp_path / "imp")
    kw = {"source_paths": {"gemini": tmp_path / "imp"}, "data_dir": tmp_path / "data"}
    gemini_zip(tmp_path / "imp" / "1.zip")
    first = sync(conn, {"gemini": c}, **kw)[0]
    assert first.status == "ok" and first.sessions_new == 2 and first.events_new == 6
    assert sync(conn, {"gemini": c}, **kw)[0].events_new == 0
    more = gemini_entries() + [gemini_entry("2026-10-02T08:00:00.000Z", "next day", "<p>yes</p>")]
    gemini_zip(tmp_path / "imp" / "2.zip", more)
    third = sync(conn, {"gemini": c}, **kw)[0]
    assert third.events_new == 2 and third.sessions_new == 1
    assert sync(conn, {"gemini": c}, full=True, **kw)[0].events_new == 0
    assert SECRET_LINE  # fixture constant used in prompts; redaction asserted above
