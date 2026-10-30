import sqlite3

import pytest

from ctx.connectors.hermes import HermesConnector
from tests.connector_contract import ConnectorContract
from tests.fixtures.hermes_db import CANARY, FAKE_TOKEN, build_hermes_home


class TestHermesContract(ConnectorContract):
    @pytest.fixture()
    def connector(self, tmp_path):
        return HermesConnector(source_root=tmp_path / "hermes")

    @pytest.fixture()
    def source_path(self, connector):
        return build_hermes_home(connector.source_root)

    @pytest.fixture()
    def bad_source_path(self, connector):
        connector.source_root.mkdir(parents=True, exist_ok=True)
        path = connector.source_root / "drifted.db"
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE schema_version (version INTEGER);
            INSERT INTO schema_version VALUES (32);
            CREATE TABLE sessions (id TEXT PRIMARY KEY, started_at REAL);
            CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, body TEXT);
            """
        )
        conn.commit()
        conn.close()
        return path

    @pytest.fixture()
    def secret_canaries(self):
        return ["abcdef123456", FAKE_TOKEN, "deepsecretvalue123", "postgres://u:p@h/db"]

    @pytest.fixture()
    def denylisted_canaries(self):
        return [CANARY]
