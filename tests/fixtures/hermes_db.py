"""Synthetic (hand-written, anonymized) Hermes state.db in the observed real shape."""

import json
import sqlite3
from pathlib import Path

FAKE_TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"  # fake, shape-only
CANARY = "CANARY-SUPERSECRET"
BIG = "A" * 70_000 + " token=deepsecretvalue123 " + "B" * 70_000
T0 = 1_790_848_800.5  # 2026-10-01T10:00:00.5Z, REAL epoch seconds as in the real DB

SESSIONS_DDL = """
CREATE TABLE sessions (
  id TEXT PRIMARY KEY, source TEXT, user_id TEXT, origin_json TEXT, model TEXT,
  system_prompt TEXT, parent_session_id TEXT, started_at REAL, ended_at REAL,
  message_count INTEGER, cwd TEXT, git_branch TEXT, title TEXT, last_activity_at REAL
);
CREATE TABLE messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT,
  tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL, token_count INTEGER,
  reasoning TEXT, reasoning_content TEXT, message_uid TEXT, _compressed_summary INTEGER,
  active INTEGER
);
CREATE TABLE schema_version (version INTEGER);
CREATE TABLE state_meta (key TEXT, value TEXT);
CREATE TABLE system_prompts (hash TEXT, prompt TEXT);
CREATE TABLE gateway_routing (scope TEXT, session_key TEXT, entry_json TEXT, updated_at REAL);
CREATE TABLE messages_fts_src (id INTEGER, content TEXT, tool_name TEXT, tool_calls TEXT);
CREATE TABLE messages_fts_data (id INTEGER, block BLOB);
CREATE TABLE messages_fts_trigram_src (id INTEGER, role TEXT, content TEXT, tool_name TEXT);
"""


def _call(call_id, name, args):
    return {
        "id": call_id,
        "call_id": call_id,
        "response_item_id": f"fc_{call_id}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def build_hermes_db(path: Path, *, wal: bool = False, offset: float = 0.0, label: str = "") -> Path:
    """One state.db. Message ids restart at 1 in every DB (profiles collide on purpose)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    c = sqlite3.connect(path)
    if wal:
        c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SESSIONS_DDL)
    c.execute("INSERT INTO schema_version VALUES (32)")
    c.execute("INSERT INTO state_meta VALUES ('k', ?)", (CANARY,))
    c.execute("INSERT INTO system_prompts VALUES ('h', ?)", (CANARY,))
    c.execute("INSERT INTO gateway_routing VALUES ('s', 'k', ?, 1)", (CANARY,))
    c.execute("INSERT INTO messages_fts_src VALUES (1, ?, 'x', 'y')", (CANARY,))
    c.execute("INSERT INTO messages_fts_data VALUES (1, ?)", (CANARY.encode(),))
    c.execute("INSERT INTO messages_fts_trigram_src VALUES (1, 'user', ?, 'x')", (CANARY,))
    t = T0 + offset

    def session(sid, cwd, started, title):
        c.execute(
            "INSERT INTO sessions VALUES (?, 'local', 'u1', ?, 'hermes-test-1', ?, NULL, ?, NULL,"
            " 0, ?, 'main', ?, NULL)",
            (sid, CANARY, CANARY, started, cwd, title),
        )

    def msg(sid, role, content, ts, **kw):
        c.execute(
            "INSERT INTO messages(session_id, role, content, tool_call_id, tool_calls, tool_name,"
            " timestamp, reasoning, reasoning_content, message_uid, _compressed_summary, active)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
            (
                sid,
                role,
                content,
                kw.get("tool_call_id"),
                json.dumps(kw["tool_calls"]) if "tool_calls" in kw else None,
                kw.get("tool_name"),
                ts,
                kw.get("reasoning"),
                kw.get("reasoning"),
                kw.get("uid"),
                kw.get("summary", 0),
            ),
        )

    s1 = f"{label}s1"
    session(s1, "/tmp/proj", t, "Zebra migration")
    msg(s1, "user", f"hello, key is api_key=abcdef123456 {FAKE_TOKEN}", t + 1, uid=f"{label}m1")
    msg(
        s1,
        "assistant",
        "Let me look.",
        t + 2,
        reasoning="plan the zebra migration",
        tool_calls=[_call("call_1", "terminal", {"command": "ls"})],
    )
    msg(
        s1,
        "tool",
        '{"output": "file1\\nfile2"}',
        t + 3,
        tool_call_id="call_1",
        tool_name="terminal",
    )
    msg(
        s1,
        "assistant",
        "",
        t + 4,
        tool_calls=[_call("call_2", "read_file", {"path": "/tmp/proj/.env"})],
    )
    msg(
        s1,
        "tool",
        "DB_URL=postgres://u:p@h/db\nPLAIN=value",
        t + 5,
        tool_call_id="call_2",
        tool_name="read_file",
    )
    msg(
        s1,
        "assistant",
        "",
        t + 6,
        tool_calls=[_call("call_3", "read_file", {"path": "/tmp/proj/big.log"})],
    )
    msg(s1, "tool", BIG, t + 7, tool_call_id="call_3", tool_name="read_file")
    msg(s1, "system", "context was compressed", t + 8)
    msg(s1, "user", "[summary of earlier turns]", t + 9, summary=1)
    msg(s1, "assistant", "done", t + 10)
    s2 = f"{label}s2"
    session(s2, None, t + 100, None)  # no cwd -> unlinked
    msg(s2, "user", "zebra planning elsewhere", t + 101)
    c.commit()
    c.close()
    return path


def build_hermes_home(root: Path, *, wal: bool = False) -> Path:
    """~/.hermes-like tree with a default DB and a `work` profile DB. Returns the root."""
    root.mkdir(parents=True, exist_ok=True)
    build_hermes_db(root / "state.db", wal=wal)
    build_hermes_db(root / "profiles" / "work" / "state.db", wal=wal, offset=5000.0)
    # denylisted files: must never be read; planted with a canary
    (root / ".env").write_text(f"OPENAI_API_KEY={CANARY}\n")
    (root / "auth.json").write_text(json.dumps({"token": CANARY}))
    (root / "config.yaml").write_text(f"secret: {CANARY}\n")
    (root / ".hermes_history").write_text(CANARY)
    (root / "profiles" / "work" / ".env").write_text(CANARY)
    return root
