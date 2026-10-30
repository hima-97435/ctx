import pytest

from ctx.connectors.codex import CodexConnector
from tests.connector_contract import ConnectorContract
from tests.fixtures.codex_rollouts import CANARY, build_codex_root, write_rollout


class TestCodexContract(ConnectorContract):
    expects_sql = False

    @pytest.fixture()
    def connector(self, tmp_path):
        return CodexConnector(source_root=tmp_path / "cx_root")

    @pytest.fixture()
    def source_path(self, connector):
        return build_codex_root(connector.source_root)

    @pytest.fixture()
    def bad_source_path(self, connector):
        sessions = connector.source_root / "sessions"
        write_rollout(
            sessions / "2026/10/01" / "rollout-old.jsonl",
            [{"id": "abc", "timestamp": "2026-10-01T10:00:00Z", "instructions": "legacy"}],
        )
        return sessions

    @pytest.fixture()
    def secret_canaries(self):
        return ["abcdef123456", "deepsecretvalue123", "postgres://u:p@h/db"]

    @pytest.fixture()
    def denylisted_canaries(self):
        return [CANARY]
