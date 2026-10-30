import pytest

from ctx.connectors.antigravity_cli import AntigravityCliConnector
from tests.connector_contract import ConnectorContract
from tests.fixtures.agy_cli import CANARY, FAKE_TOKEN, build_agy_root, step, write_jsonl


class TestAntigravityCliContract(ConnectorContract):
    @pytest.fixture()
    def connector(self, tmp_path):
        return AntigravityCliConnector(source_root=tmp_path / "agy", env={})

    @pytest.fixture()
    def source_path(self, connector):
        return build_agy_root(connector.source_root)

    @pytest.fixture()
    def bad_source_path(self, connector):
        bad = step(0, "USER_INPUT", "USER_EXPLICIT", content="x")
        del bad["step_index"]
        root = connector.source_root / "drifted"
        write_jsonl(
            root / "brain" / "x" / ".system_generated" / "logs" / "transcript_full.jsonl", [bad]
        )
        return root

    @pytest.fixture()
    def secret_canaries(self):
        return ["abcdef123456", FAKE_TOKEN, "deepsecretvalue123", "postgres://u:p@h/db"]

    @pytest.fixture()
    def denylisted_canaries(self):
        return [CANARY]
