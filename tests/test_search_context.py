from datetime import UTC, datetime

import pytest

from ctx.core.context import build_context
from ctx.core.db import connect
from ctx.core.projects import load_projects_config
from ctx.core.search import fts_query, parse_when, resolve_project, search
from ctx.core.sync import sync
from ctx.core.truncate import truncate_to_tokens
from tests.helpers import PROJECTS_TOML, FakeConnector, make_opencode_root, raw_event


@pytest.fixture()
def conn(tmp_path):
    connector = make_opencode_root(tmp_path / "oc")
    cfg = tmp_path / "projects.toml"
    cfg.write_text(PROJECTS_TOML)
    c = connect(tmp_path / "s.db")
    sync(
        c,
        {"opencode": connector},
        project_configs=load_projects_config(cfg),
        data_dir=tmp_path / "data",
    )
    return c


def sessions_of(hits):
    return sorted(h.citation.source_session_id for h in hits)


def test_search_hides_non_allowed_and_unlinked_by_default(conn):
    # ses_3 (project not allowed) and ses_4 (no project) are hidden
    assert sessions_of(search(conn, "zebra", limit=50)) == ["ses_2"]
    assert sessions_of(search(conn, "zebra", include_all=True, limit=50)) == [
        "ses_2",
        "ses_3",
        "ses_4",
    ]


def test_search_project_filter_by_id_and_name(conn):
    assert sessions_of(search(conn, "zebra", project="sub")) == ["ses_2"]
    assert sessions_of(search(conn, "zebra", project="Sub Project")) == ["ses_2"]
    assert sessions_of(search(conn, "zebra", project="proj")) == []
    assert sessions_of(search(conn, "zebra", project="hidden")) == []  # not allowed
    assert sessions_of(search(conn, "zebra", project="hidden", include_all=True)) == ["ses_3"]
    with pytest.raises(ValueError, match="unknown project"):
        search(conn, "zebra", project="nope")
    assert resolve_project(conn, "SUB PROJECT") == "sub"


def test_search_source_and_time_filters(conn):
    assert search(conn, "zebra", source="other", include_all=True) == []
    assert len(search(conn, "zebra", source="opencode", include_all=True, limit=50)) == 3
    assert sessions_of(search(conn, "zebra", since=5500, include_all=True)) == ["ses_3", "ses_4"]
    assert sessions_of(search(conn, "zebra", until=5500, include_all=True)) == ["ses_2"]
    assert search(conn, "zebra", since=5000, until=5000, include_all=True)[0].citation.ts == 5000


def test_search_citation_and_snippet(conn):
    hit = search(conn, "zebra", project="sub")[0]
    c = hit.citation
    assert c.source == "opencode"
    assert c.session_id == "opencode:ses_2" and c.source_session_id == "ses_2"
    assert c.message_id == "msg_ses_2" and c.source_id == "prt_ses_2"
    assert c.ts == 5000 and c.project_id == "sub" and c.event_id > 0
    assert "«zebra»" in hit.snippet
    assert hit.kind == "text" and hit.role == "user" and hit.text is None


def test_search_porter_stemming_and_limit(conn):
    assert search(conn, "migrations", include_all=True, limit=50)  # stems to migration
    assert len(search(conn, "zebra", include_all=True, limit=1)) == 1


def test_search_redacted_text_only(conn):
    assert search(conn, "abcdef123456", include_all=True) == []
    assert search(conn, "REDACTED", include_all=True)


@pytest.mark.parametrize("query", ['foo"bar', "a-b", "x:y", "AND OR NOT", "(a*", '"unterminated'])
def test_hostile_queries_do_not_crash(conn, query):
    search(conn, query, include_all=True)


def test_empty_query_rejected(conn):
    for q in ("", "   ", "!!!"):
        with pytest.raises(ValueError, match="empty"):
            search(conn, q)


def test_fts_query_quoting():
    assert fts_query('hello "big world" x') == '"hello" "big world" "x"'


def test_parse_when():
    now = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
    assert parse_when("1d", now=now) == int(
        datetime(2026, 10, 30, 12, tzinfo=UTC).timestamp() * 1000
    )
    assert parse_when("2w", now=now) < parse_when("1w", now=now)
    assert parse_when("2026-10-01") == int(datetime(2026, 10, 1, tzinfo=UTC).timestamp() * 1000)
    assert parse_when("2026-10-01", end_of_day=True) - parse_when("2026-10-01") == 86_399_999
    assert parse_when("2026-10-01T09:30") == int(
        datetime(2026, 10, 1, 9, 30, tzinfo=UTC).timestamp() * 1000
    )
    assert parse_when("2026-10-01T09:30+02:00", end_of_day=True) == int(
        datetime(2026, 10, 1, 7, 30, tzinfo=UTC).timestamp() * 1000
    )
    with pytest.raises(ValueError, match="cannot parse"):
        parse_when("yesterday-ish")


# --- build_context ---------------------------------------------------------------------


@pytest.fixture()
def ctx_conn(tmp_path):
    events = [
        raw_event("big", "alpha " + "filler " * 400, ts=1),  # ~700 tokens
        raw_event("mid", "alpha " + "pad " * 100, ts=2),  # ~100 tokens
        raw_event("small", "alpha tiny", ts=3),
    ]
    fake = FakeConnector(tmp_path / "fr", events)
    fake.root.mkdir()
    c = connect(tmp_path / "c.db")
    from ctx.core.projects import ProjectConfig

    sync(
        c,
        {"fake": fake},
        project_configs=[ProjectConfig(id="p", name="P", allowed=True, roots=["/tmp/proj"])],
        data_dir=tmp_path / "d",
    )
    return c


def test_build_context_within_budget_has_evidence_and_leftovers(ctx_conn):
    pkg = build_context(ctx_conn, "alpha", 150, project="p")
    assert pkg.tokens_used <= 150
    assert pkg.items and all(i.evidence_event_ids and i.citation for i in pkg.items)
    ids_in = {e for i in pkg.items for e in i.evidence_event_ids}
    ids_out = {lo.event_id for lo in pkg.left_out}
    assert ids_in.isdisjoint(ids_out)
    assert len(ids_in | ids_out) == 3  # everything accounted for
    assert all(lo.reason == "over_budget" for lo in pkg.left_out)
    assert pkg.evidence_event_ids == [e for i in pkg.items for e in i.evidence_event_ids]
    assert sum(i.token_estimate for i in pkg.items) == pkg.tokens_used


def test_build_context_large_budget_keeps_all(ctx_conn):
    pkg = build_context(ctx_conn, "alpha", 10_000)
    assert len(pkg.items) == 3 and pkg.left_out == []
    assert not any(i.truncated for i in pkg.items)


def test_build_context_truncates_best_hit_when_nothing_fits(ctx_conn):
    pkg = build_context(ctx_conn, "alpha", 20)
    assert pkg.items, "should return a trimmed top hit rather than nothing"
    assert pkg.tokens_used <= 20
    first = pkg.items[0]
    if first.truncated:
        assert "truncated" in first.text


def test_build_context_no_match_and_zero_budget(ctx_conn):
    empty = build_context(ctx_conn, "nonexistentterm", 100)
    assert empty.items == [] and empty.left_out == [] and empty.tokens_used == 0
    zero = build_context(ctx_conn, "alpha", 0)
    assert zero.items == [] and len(zero.left_out) == 3


def test_build_context_render_has_citations_and_respects_privacy(conn):
    pkg = build_context(conn, "zebra", 1000)
    text = pkg.render()
    assert "opencode session=ses_2 message=msg_ses_2" in text
    assert "ses_3" not in text  # non-allowed project excluded
    assert "ses_3" in build_context(conn, "zebra", 1000, include_all=True).render()


def test_build_context_redacts_defensively(ctx_conn):
    # simulate a legacy/unredacted row slipping into events
    ctx_conn.execute(
        "INSERT INTO events(source, source_id, ts, project_id, kind, text, token_estimate,"
        " content_hash) VALUES ('x','leak',9,'p','text','omega api_key=supersecret12345',5,'h')"
    )
    pkg = build_context(ctx_conn, "omega", 100, project="p")
    assert "supersecret12345" not in pkg.render()


def test_truncate_to_tokens():
    text = "a" * 400
    assert truncate_to_tokens(text, 100) == text
    out = truncate_to_tokens(text, 20)
    assert len(out) // 4 <= 20 and "truncated" in out and out.startswith("a")
    assert len(truncate_to_tokens(text, 2)) <= 8
    assert truncate_to_tokens(text, 0) == ""
