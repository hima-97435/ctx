import sqlite3

import pytest

from ctx.connectors.devin import DevinConnector
from tests.connector_contract import ConnectorContract
from tests.fixtures.devin_db import CANARY, FAKE_TOKEN, build_devin_root


class TestDevinContract(ConnectorContract):
    @pytest.fixture()
    def connector(self, tmp_path):
        return DevinConnector(source_root=tmp_path / "devin")

    @pytest.fixture()
    def source_path(self, connector):
        return build_devin_root(connector.source_root)

    @pytest.fixture()
    def bad_source_path(self, connector):
        path = connector.source_root / "cli" / "drifted.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE refinery_schema_history (version INTEGER, name TEXT);
            CREATE TABLE sessions (id TEXT PRIMARY KEY, working_directory TEXT);
            CREATE TABLE message_nodes (session_id TEXT, node_id INTEGER, body TEXT);
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
