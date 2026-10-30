"""Registered connectors. The only place the CLI/engine learn which sources exist.

`CTX_SOURCE_ROOT_<NAME>` (e.g. CTX_SOURCE_ROOT_CLAUDE_CODE) overrides a
connector's source root — used by tests and sandboxed runs; the override becomes that
connector's sole allowed path.
"""

import os
from pathlib import Path

from ctx.connectors.antigravity_cli import AntigravityCliConnector
from ctx.connectors.base import Connector
from ctx.connectors.claude_code import ClaudeCodeConnector
from ctx.connectors.codex import CodexConnector
from ctx.connectors.devin import DevinConnector
from ctx.connectors.hermes import HermesConnector
from ctx.connectors.opencode import OpenCodeConnector


def available_connectors() -> dict[str, Connector]:
    def root_for(name: str) -> Path | None:
        override = os.environ.get(f"CTX_SOURCE_ROOT_{name.upper()}")
        return Path(override) if override else None

    connectors: list[Connector] = [
        OpenCodeConnector(root_for("opencode")),
        ClaudeCodeConnector(root_for("claude_code")),
        CodexConnector(root_for("codex")),
        HermesConnector(root_for("hermes")),
        DevinConnector(root_for("devin")),
        AntigravityCliConnector(root_for("antigravity_cli")),
    ]
    return {c.name: c for c in connectors}
