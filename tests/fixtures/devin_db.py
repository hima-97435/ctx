"""Synthetic (hand-written, anonymized) Devin CLI sessions.db in the observed real shape."""

import json
import sqlite3
from pathlib import Path

FAKE_TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"  # fake, shape-only
CANARY = "CANARY-SUPERSECRET"
BIG = "A" * 70_000 + " token=deepsecretvalue123 " + "B" * 70_000
T0 = 1_790_848_800  # 2026-10-01T10:00:00Z, INTEGER epoch seconds

DDL = """
CREATE TABLE sessions (
  id TEXT PRIMARY KEY, working_directory TEXT, backend_type TEXT, model TEXT, agent_mode TEXT,
  created_at INTEGER, last_activity_at INTEGER, title TEXT, main_chain_id INTEGER, cogs_json TEXT,
  workspace_dirs TEXT, hidden INTEGER, metadata TEXT
);
CREATE TABLE message_nodes (
  row_id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, node_id INTEGER,
  parent_node_id INTEGER, chat_message TEXT, created_at INTEGER, metadata TEXT
);
CREATE TABLE tool_call_state (
  session_id TEXT, tool_call_id TEXT, tool_call_json TEXT, tool_call_update_json TEXT,
  source_node_ids TEXT
);
CREATE TABLE refinery_schema_history (
  version int4 PRIMARY KEY, name VARCHAR(255), applied_on VARCHAR(255), checksum VARCHAR(255)
);
CREATE TABLE app_state (key TEXT, value TEXT);
CREATE TABLE prompt_history (id INTEGER, content TEXT, timestamp INTEGER, session_id TEXT);
CREATE TABLE message_search_data (id INTEGER, block BLOB);
CREATE TABLE live_row_times (session_id TEXT, row_key TEXT, started_at TEXT);
"""


def iso(offset: int) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(T0 + offset, UTC).strftime("%Y-%m-%dT%H:%M:%S.000000Z")


def node_json(message_id, role, content, offset, **extra):
    return {
        "content": content,
        "message_id": message_id,
        "metadata": {"created_at": iso(offset), "finish_reason": None, "is_user_input": None},
        "role": role,
        **extra,
    }


def build_devin_db(path: Path, *, wal: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    c = sqlite3.connect(path)
    if wal:
        c.execute("PRAGMA journal_mode=WAL")
    c.executescript(DDL)
    c.execute("INSERT INTO refinery_schema_history VALUES (20, 'live_row_times', 'x', 'y')")
    c.execute("INSERT INTO app_state VALUES ('k', ?)", (CANARY,))
    c.execute("INSERT INTO prompt_history VALUES (1, ?, 1, 'sess-a')", (CANARY,))
    c.execute("INSERT INTO message_search_data VALUES (1, ?)", (CANARY.encode(),))
    c.execute("INSERT INTO tool_call_state VALUES ('sess-a', 'tc_1', ?, ?, NULL)", (CANARY, CANARY))
    sessions = (
        ("sess-a", "/tmp/proj", "Zebra migration", 0, 60),
        ("sess-b", "/tmp/proj/sub/deep", "Other", 100, 110),
        ("sess-c", "", None, 200, 210),  # no working directory -> unlinked
    )
    for sid, cwd, title, a, b in sessions:
        c.execute(
            "INSERT INTO sessions VALUES (?, ?, 'windsurf', 'devin-test-1', 'normal', ?, ?, ?,"
            " NULL, ?, '[]', 0, NULL)",
            (sid, cwd, T0 + a, T0 + b, title, CANARY),
        )

    def node(sid, nid, parent, obj, offset):
        c.execute(
            "INSERT INTO message_nodes(session_id, node_id, parent_node_id, chat_message,"
            " created_at, metadata) VALUES (?, ?, ?, ?, ?, NULL)",
            (sid, nid, parent, json.dumps(obj) if not isinstance(obj, str) else obj, T0 + offset),
        )

    a = "sess-a"
    node(a, 1, None, node_json("sys1", "system", "You are a coding agent.", 0), 0)
    node(
        a,
        2,
        1,
        node_json("u1", "user", f"hello, key is api_key=abcdef123456 {FAKE_TOKEN}", 1),
        1,
    )
    node(
        a,
        3,
        2,
        node_json(
            "a1",
            "assistant",
            "Let me look.",
            2,
            thinking="plan the zebra migration",
            tool_calls=[
                {
                    "id": "tc_1",
                    "index": 0,
                    "kind": "execute",
                    "name": "exec",
                    "arguments": {"command": "ls"},
                }
            ],
        ),
        2,
    )
    node(a, 4, 3, node_json("t1", "tool", "file1\nfile2", 3, tool_call_id="tc_1"), 3)
    node(
        a,
        5,
        4,
        node_json(
            "a2",
            "assistant",
            "",
            4,
            tool_calls=[
                {
                    "id": "tc_2",
                    "index": 0,
                    "kind": "read",
                    "name": "read",
                    "arguments": {"file_path": "/tmp/proj/.env"},
                }
            ],
        ),
        4,
    )
    node(
        a,
        6,
        5,
        node_json("t2", "tool", "DB_URL=postgres://u:p@h/db\nPLAIN=value", 5, tool_call_id="tc_2"),
        5,
    )
    node(
        a,
        7,
        6,
        node_json(
            "a3",
            "assistant",
            "",
            6,
            tool_calls=[
                {
                    "id": "tc_3",
                    "index": 0,
                    "kind": "read",
                    "name": "read",
                    "arguments": {"file_path": "/tmp/proj/big.log"},
                }
            ],
        ),
        6,
    )
    node(a, 8, 7, node_json("t3", "tool", BIG, 7, tool_call_id="tc_3"), 7)
    node(
        a,
        9,
        8,
        node_json("u2", "user", "see image", 8, images=[{"mime": "image/png", "data": "IMGBYTES"}]),
        8,
    )
    # compaction re-inserts earlier messages under new node ids (same message_id): first wins
    node(a, 10, None, node_json("u1", "user", "REWRITTEN COPY of the first message", 1), 20)
    node(a, 11, 10, node_json("a1", "assistant", "REWRITTEN", 2), 20)
    node(a, 12, 11, node_json("a4", "assistant", "done", 21), 21)
    node(a, 13, 12, "{this is not json", 22)  # malformed row: counted, not fatal
    node(a, 14, 13, node_json("x1", "developer", "unknown role", 23), 23)
    b = "sess-b"
    node(b, 1, None, node_json("u1", "user", "zebra planning elsewhere", 100), 100)
    node(b, 2, 1, node_json("a1", "assistant", "ok", 101), 101)  # same message_id as sess-a: ok
    c.commit()
    c.close()
    return path


def build_devin_root(root: Path, *, wal: bool = False) -> Path:
    """~/.local/share/devin-like tree. Returns the sessions.db path."""
    db = build_devin_db(root / "cli" / "sessions.db", wal=wal)
    (root / "credentials.toml").write_text(f'token = "{CANARY}"\n')
    (root / "mcp" / "oauth").mkdir(parents=True, exist_ok=True)
    (root / "mcp" / "oauth" / "server.json").write_text(CANARY)
    (root / "cli" / "transcripts").mkdir(parents=True, exist_ok=True)
    (root / "cli" / "transcripts" / "sess-a.json").write_text(json.dumps({"steps": [CANARY]}))
    return db
