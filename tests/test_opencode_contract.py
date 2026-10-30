import sqlite3

import pytest

from ctx.connectors.opencode import OpenCodeConnector
from tests.connector_contract import ConnectorContract
from tests.fixtures.opencode_db import build_opencode_db


class TestOpenCodeContract(ConnectorContract):
    @pytest.fixture()
    def connector(self, tmp_path):
        return OpenCodeConnector(source_root=tmp_path / "oc_root")

    @pytest.fixture()
    def source_path(self, connector):
        connector.source_root.mkdir(parents=True, exist_ok=True)
        return build_opencode_db(connector.default_source_path())

    @pytest.fixture()
    def bad_source_path(self, connector):
        connector.source_root.mkdir(parents=True, exist_ok=True)
        path = connector.source_root / "drifted.db"
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE __drizzle_migrations (id INTEGER PRIMARY KEY, hash TEXT);
            CREATE TABLE session (id TEXT PRIMARY KEY, directory TEXT);
            CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, data TEXT);
            CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT);
            """
        )
        conn.commit()
        conn.close()
        return path

    @pytest.fixture()
    def secret_canaries(self):
        return ["abcdef123456", "deepsecretvalue123", "abcdefgh123", "postgres://u:p@h/db"]

    @pytest.fixture()
    def denylisted_canaries(self):
        return ["CANARY-SUPERSECRET"]
