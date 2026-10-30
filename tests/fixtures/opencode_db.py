"""Builds a small anonymized OpenCode DB in the real schema (shared by tests)."""

import json
import sqlite3
from pathlib import Path


def build_opencode_db(path: Path) -> Path:
    """Small anonymized sample in the real OpenCode schema (incl. a canary
    denylisted table that must never be read)."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE __drizzle_migrations (
          id INTEGER PRIMARY KEY, hash TEXT, created_at NUMERIC,
          name TEXT, applied_at TEXT
        );
        CREATE TABLE session (
          id TEXT PRIMARY KEY, project_id TEXT, directory TEXT, title TEXT,
          version TEXT, agent TEXT, model TEXT,
          time_created INTEGER, time_updated INTEGER
        );
        CREATE TABLE message (
          id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER,
          time_updated INTEGER, data TEXT
        );
        CREATE TABLE part (
          id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
          time_created INTEGER, time_updated INTEGER, data TEXT
        );
        CREATE TABLE credential (id TEXT PRIMARY KEY, value TEXT);
        """
    )
    conn.execute(
        "INSERT INTO __drizzle_migrations VALUES"
        " (1, NULL, 0, '20260423070820_add_icon_url_override', 'x')"
    )
    model = json.dumps({"id": "glm-5.3-flash", "providerID": "nvidia"})
    conn.execute(
        "INSERT INTO session VALUES"
        " (?, 'p1', '/tmp/proj', 'Title', '1.18.34', 'build', ?, 1000, 2000)",
        ("ses_1", model),
    )
    conn.execute(
        "INSERT INTO message VALUES ('msg_1', 'ses_1', 1000, 1000, '{\"role\": \"user\"}')"
    )
    conn.execute(
        "INSERT INTO message VALUES ('msg_2', 'ses_1', 1100, 1100, '{\"role\": \"assistant\"}')"
    )
    big_output = "A" * 70_000 + " token=deepsecretvalue123 " + "B" * 70_000
    text_part = {"type": "text", "text": "hello with api_key=abcdef123456 inside"}
    bash_part = {
        "type": "tool",
        "tool": "bash",
        "callID": "c1",
        "state": {"status": "completed", "input": {"command": "ls"}, "output": "file1\nfile2"},
    }
    big_read_part = {
        "type": "tool",
        "tool": "read",
        "state": {"status": "completed", "input": {"path": "/x/big.txt"}, "output": big_output},
    }
    parts = [
        ("prt_1", "msg_1", 1000, text_part),
        ("prt_2", "msg_2", 1100, bash_part),
        ("prt_3", "msg_2", 1200, {"type": "step-start"}),
        ("prt_4", "msg_2", 1300, big_read_part),
        (
            "prt_5",
            "msg_2",
            1400,
            {"type": "compaction", "auto": True, "tail_start_id": "msg_x"},
        ),
        (
            "prt_6",
            "msg_2",
            1500,
            {"type": "file", "filename": "a.ipynb", "mime": "text/plain", "url": "file:///a.ipynb"},
        ),
        (
            "prt_7",
            "msg_2",
            1600,
            {
                "type": "tool",
                "tool": "read",
                "state": {
                    "status": "completed",
                    "input": {"path": "/x/.env"},
                    "output": "SECRET_TOKEN=abcdefgh123\nDATABASE_URL=postgres://u:p@h/db\n",
                },
            },
        ),
    ]
    for pid, mid, ts, data in parts:
        conn.execute(
            "INSERT INTO part VALUES (?, ?, 'ses_1', ?, ?, ?)",
            (pid, mid, ts, ts, json.dumps(data)),
        )
    conn.execute("INSERT INTO credential VALUES ('c1', 'CANARY-SUPERSECRET')")
    conn.commit()
    conn.close()
    return path
