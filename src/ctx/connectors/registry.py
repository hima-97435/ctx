"""Registered connectors. The only place the CLI/engine learn which sources exist.

`CTX_SOURCE_ROOT_<NAME>` (e.g. CTX_SOURCE_ROOT_OPENCODE) overrides a connector's
source root — used by tests and sandboxed runs; the override becomes that
connector's sole allowed path.
"""

import os
from pathlib import Path

from ctx.connectors.base import Connector
from ctx.connectors.opencode import OpenCodeConnector


def available_connectors() -> dict[str, Connector]:
    def root_for(name: str) -> Path | None:
        override = os.environ.get(f"CTX_SOURCE_ROOT_{name.upper()}")
        return Path(override) if override else None

    connectors: list[Connector] = [OpenCodeConnector(root_for("opencode"))]
    return {c.name: c for c in connectors}
