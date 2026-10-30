"""Print-only MCP client configuration snippets (AGENTS rule 10: never write user config)."""

import json

CLIENTS = ("claude-code", "codex", "opencode", "cursor", "gemini", "hermes", "generic")

_STDIO_JSON = {"mcpServers": {"ctx": {"command": "ctx", "args": ["serve-mcp"]}}}


def snippet(client: str) -> str:
    """Return the instructions for `client`. Unverified formats are labeled as such."""
    if client == "claude-code":
        return "# Run once (verified):\nclaude mcp add ctx -- ctx serve-mcp"
    if client == "codex":
        return (
            "# UNVERIFIED format. Add to ~/.codex/config.toml\n"
            "# (or try: codex mcp add ctx -- ctx serve-mcp)\n"
            '[mcp_servers.ctx]\ncommand = "ctx"\nargs = ["serve-mcp"]'
        )
    if client == "opencode":
        body = {
            "$schema": "https://opencode.ai/config.json",
            "mcp": {"ctx": {"type": "local", "command": ["ctx", "serve-mcp"], "enabled": True}},
        }
        return "# UNVERIFIED format. Merge into opencode.json:\n" + json.dumps(body, indent=2)
    if client == "cursor":
        return "# UNVERIFIED format. Merge into ~/.cursor/mcp.json:\n" + json.dumps(
            _STDIO_JSON, indent=2
        )
    if client == "gemini":
        return (
            "# UNVERIFIED format. Merge into ~/.gemini/settings.json "
            "(or try: gemini mcp add ctx ctx serve-mcp):\n" + json.dumps(_STDIO_JSON, indent=2)
        )
    if client == "hermes":
        return (
            "# UNVERIFIED format. Add to ~/.hermes/config.yaml:\n"
            "mcp_servers:\n  ctx:\n    command: ctx\n    args: [serve-mcp]"
        )
    if client == "generic":
        return (
            "# Generic stdio MCP server: command `ctx`, args `serve-mcp`. Common JSON shape:\n"
            + json.dumps(_STDIO_JSON, indent=2)
        )
    raise ValueError(f"unknown client {client!r}; choose from: {', '.join(CLIENTS)}")
