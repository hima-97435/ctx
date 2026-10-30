import pytest

from ctx.connectors.claude_code import ClaudeCodeConnector
from tests.connector_contract import ConnectorContract
from tests.fixtures.claude_code_jsonl import (
    CANARY,
    FAKE_TOKEN,
    build_claude_root,
    user_text,
    write_jsonl,
)


class TestClaudeCodeContract(ConnectorContract):
    expects_sql = False

    @pytest.fixture()
    def connector(self, tmp_path):
        return ClaudeCodeConnector(source_root=tmp_path / "cc_root")

    @pytest.fixture()
    def source_path(self, connector):
        return build_claude_root(connector.source_root)

    @pytest.fixture()
    def bad_source_path(self, connector):
        bad = user_text("u1", None, "2026-10-01T10:00:00Z", "x")
        del bad["message"]
        projects = connector.source_root / "projects"
        write_jsonl(projects / "-tmp-bad" / "drift.jsonl", [bad])
        return projects

    @pytest.fixture()
    def secret_canaries(self):
        return ["abcdef123456", FAKE_TOKEN, "deepsecretvalue123", "postgres://u:p@h/db"]

    @pytest.fixture()
    def denylisted_canaries(self):
        return [CANARY]
