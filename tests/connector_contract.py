"""Reusable connector contract suite (AGENTS rule 6, docs/ARCHITECTURE.md).

Usage: subclass `ConnectorContract` in a `test_*.py` and provide the fixtures
below. Every connector must pass every test here.

Fixtures a subclass supplies:
- connector:           a Connector whose allowed_paths include tmp locations
- source_path:         a valid real-format source inside the allowed paths
- bad_source_path:     a source inside the allowed paths with an unrecognized
                       schema/version
- secret_canaries:     strings present in the fixture's source content that
                       redaction must remove before they reach events/blobs/FTS
- denylisted_canaries: strings stored only in denylisted tables/files; they must
                       never appear anywhere in the output
"""

import re
import sqlite3

import pytest

from ctx.connectors.base import ConnectorError, DenylistViolation
from ctx.core.blobs import BlobStore
from ctx.core.db import connect
from ctx.core.model import RawEvent, RawSession
from ctx.core.search import search
from ctx.core.sync import sync


def _fetch(connector, source_path, tmp_path, blobs=None):
    store = blobs or BlobStore(tmp_path / "contract_blobs")
    records = []
    for record in connector.fetch(source_path, blob_store=store, tmp_dir=tmp_path / "contract_tmp"):
        if isinstance(record, RawEvent) and record.blob_ref:
            # blob-before-event: it must exist by the time the event is yielded
            assert store.exists(record.blob_ref), "blob not on disk before its event"
        records.append(record)
    return records, store


class ConnectorContract:
    # Set False for connectors that do not read SQLite (no SQL trace to inspect).
    expects_sql = True

    # ---- fixtures to override -------------------------------------------------
    @pytest.fixture()
    def connector(self):
        raise NotImplementedError

    @pytest.fixture()
    def source_path(self):
        raise NotImplementedError

    @pytest.fixture()
    def bad_source_path(self):
        raise NotImplementedError

    @pytest.fixture()
    def secret_canaries(self) -> list[str]:
        raise NotImplementedError

    @pytest.fixture()
    def denylisted_canaries(self) -> list[str]:
        raise NotImplementedError

    # ---- declarations ---------------------------------------------------------
    def test_declares_required_attributes(self, connector):
        assert isinstance(connector.name, str) and connector.name
        assert isinstance(connector.schema_version_pin, str) and connector.schema_version_pin
        assert len(connector.allowed_paths) >= 1
        assert all(hasattr(p, "resolve") for p in connector.allowed_paths)
        assert all(isinstance(p, str) for p in connector.denylist_paths)
        assert all(isinstance(t, str) for t in connector.denylist_tables)
        assert connector.denylist_paths or connector.denylist_tables

    def test_check_version_returns_marker(self, connector, source_path):
        assert connector.check_version(source_path)

    # ---- fail loud / permissions ---------------------------------------------
    def test_version_mismatch_fails_loud_before_any_output(
        self, connector, bad_source_path, tmp_path
    ):
        with pytest.raises(ConnectorError):
            connector.check_version(bad_source_path)
        yielded = []
        with pytest.raises(ConnectorError):
            for record in connector.fetch(
                bad_source_path,
                blob_store=BlobStore(tmp_path / "b"),
                tmp_dir=tmp_path / "t",
            ):
                yielded.append(record)
        assert yielded == []

    def test_version_mismatch_writes_nothing_via_engine(self, connector, bad_source_path, tmp_path):
        conn = connect(tmp_path / "bad.db")
        results = sync(
            conn,
            {connector.name: connector},
            source_paths={connector.name: bad_source_path},
            data_dir=tmp_path / "bad_data",
        )
        assert results[0].status == "error"
        assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
        assert not (tmp_path / "bad_data" / "blobs").exists()
        row = conn.execute("SELECT schema_version_seen FROM sources").fetchone()
        assert row["schema_version_seen"] is None

    def test_refuses_denylisted_paths(self, connector, tmp_path):
        for pattern in connector.denylist_paths:
            name = pattern.replace("**/", "").replace("*", "x")
            target = connector.allowed_paths[0] / name
            with pytest.raises(DenylistViolation):
                connector.check_version(target)
            with pytest.raises(DenylistViolation):
                list(
                    connector.fetch(
                        target, blob_store=BlobStore(tmp_path / "b"), tmp_dir=tmp_path / "t"
                    )
                )

    def test_engine_refuses_denylisted_and_outside_paths(self, connector, tmp_path):
        conn = connect(tmp_path / "deny.db")
        bad_targets = [
            connector.allowed_paths[0] / pattern.replace("**/", "").replace("*", "x")
            for pattern in connector.denylist_paths
        ]
        bad_targets.append(tmp_path / "elsewhere" / "outside.db")
        for target in bad_targets:
            results = sync(
                conn,
                {connector.name: connector},
                source_paths={connector.name: target},
                data_dir=tmp_path / "deny_data",
            )
            assert results[0].status == "error"
            assert "Violation" in results[0].error or "outside allowed root" in results[0].error
        assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 0

    def test_never_queries_denylisted_tables(self, connector, source_path, tmp_path, monkeypatch):
        statements: list[str] = []
        real_connect = sqlite3.connect

        def tracing_connect(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            conn.set_trace_callback(statements.append)
            return conn

        monkeypatch.setattr(sqlite3, "connect", tracing_connect)
        _fetch(connector, source_path, tmp_path)
        monkeypatch.undo()
        if not self.expects_sql:
            assert not statements, "file-based connector must not open SQLite"
            return
        assert statements, "expected to observe SQL for a sqlite-backed connector"
        for table in connector.denylist_tables:
            pattern = re.compile(rf"\b{re.escape(table)}\b", re.IGNORECASE)
            assert not [s for s in statements if pattern.search(s)], f"queried {table}"

    # ---- record shape -----------------------------------------------------------
    def test_records_are_well_formed(self, connector, source_path, tmp_path):
        records, store = _fetch(connector, source_path, tmp_path)
        sessions = [r for r in records if isinstance(r, RawSession)]
        events = [r for r in records if isinstance(r, RawEvent)]
        assert sessions and events
        first_event = next(i for i, r in enumerate(records) if isinstance(r, RawEvent))
        assert all(isinstance(r, RawSession) for r in records[:first_event]), (
            "sessions must precede events"
        )
        session_ids = {s.source_session_id for s in sessions}
        assert len({s.source_session_id for s in sessions}) == len(sessions)
        assert len({e.source_id for e in events}) == len(events), "source_id must be unique"
        for e in events:
            assert e.source == connector.name
            assert e.source_id
            assert isinstance(e.token_estimate, int) and e.token_estimate >= 0
            assert re.fullmatch(r"[0-9a-f]{64}", e.content_hash)
            assert e.kind
            if e.session_ref:
                assert e.session_ref in session_ids
            assert bool(e.blob_ref) == e.truncated
            if e.blob_ref:
                assert re.fullmatch(r"blobs/[0-9a-f]{2}/[0-9a-f]{64}", e.blob_ref)

    def test_fetch_is_deterministic(self, connector, source_path, tmp_path):
        a, _ = _fetch(connector, source_path, tmp_path / "a")
        b, _ = _fetch(connector, source_path, tmp_path / "b")
        assert a == b

    # ---- redaction ----------------------------------------------------------------
    def test_secrets_and_denylisted_content_never_in_output(
        self, connector, source_path, tmp_path, secret_canaries, denylisted_canaries
    ):
        records, store = _fetch(connector, source_path, tmp_path)
        blob_text = "".join(
            store.read(r.blob_ref) for r in records if isinstance(r, RawEvent) and r.blob_ref
        )
        haystack = blob_text + "\n".join(repr(r) for r in records)  # every field of every record
        for canary in secret_canaries + denylisted_canaries:
            assert canary not in haystack, "canary leaked from connector output"

    def test_redaction_reaches_db_and_fts(
        self, connector, source_path, tmp_path, secret_canaries, denylisted_canaries
    ):
        conn = connect(tmp_path / "red.db")
        sync(
            conn,
            {connector.name: connector},
            source_paths={connector.name: source_path},
            data_dir=tmp_path / "red_data",
        )
        dump = "\n".join(
            str(tuple(row)) for row in conn.execute("SELECT text, raw_json, blob_ref FROM events")
        )
        for canary in secret_canaries + denylisted_canaries:
            assert canary not in dump
            words = re.findall(r"\w+", canary)
            if words:
                assert search(conn, " ".join(words), include_all=True) == []

    # ---- idempotency ---------------------------------------------------------------
    def test_resync_creates_zero_duplicates(self, connector, source_path, tmp_path):
        conn = connect(tmp_path / "idem.db")
        kwargs = {
            "source_paths": {connector.name: source_path},
            "data_dir": tmp_path / "idem_data",
        }
        first = sync(conn, {connector.name: connector}, **kwargs)[0]
        assert first.status == "ok" and first.events_new > 0
        snapshot = conn.execute(
            "SELECT id, source_id, content_hash FROM events ORDER BY id"
        ).fetchall()
        sessions = conn.execute("SELECT count(*) FROM sessions").fetchone()[0]
        second = sync(conn, {connector.name: connector}, **kwargs)[0]
        assert second.status == "ok"
        assert second.events_new == 0 and second.sessions_new == 0
        assert second.events_duplicate == first.events_new
        again = conn.execute(
            "SELECT id, source_id, content_hash FROM events ORDER BY id"
        ).fetchall()
        assert [tuple(r) for r in again] == [tuple(r) for r in snapshot]
        assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == sessions
        distinct = conn.execute(
            "SELECT count(DISTINCT source || '/' || source_id) FROM events"
        ).fetchone()[0]
        assert distinct == len(snapshot)

    def test_every_stored_event_has_token_estimate_and_existing_blob(
        self, connector, source_path, tmp_path
    ):
        conn = connect(tmp_path / "te.db")
        data = tmp_path / "te_data"
        sync(
            conn,
            {connector.name: connector},
            source_paths={connector.name: source_path},
            data_dir=data,
        )
        for row in conn.execute("SELECT token_estimate, blob_ref FROM events"):
            assert row["token_estimate"] is not None and row["token_estimate"] >= 0
            if row["blob_ref"]:
                assert (data / row["blob_ref"]).exists()
