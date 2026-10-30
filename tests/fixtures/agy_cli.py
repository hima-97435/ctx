"""Synthetic (hand-written, anonymized) Antigravity CLI tree in the observed real shape."""

import json
import sqlite3
from pathlib import Path

FAKE_TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"  # fake, shape-only
CANARY = "CANARY-SUPERSECRET"
BIG = "A" * 70_000 + " token=deepsecretvalue123 " + "B" * 70_000
CONV_1 = "aaaaaaaa-1111-4111-8111-111111111111"
CONV_2 = "bbbbbbbb-2222-4222-8222-222222222222"
CONV_OPAQUE = "cccccccc-3333-4333-8333-333333333333"  # protobuf only, no transcript
CONV_LIVE = "dddddddd-4444-4444-8444-444444444444"  # last step still RUNNING


def ts(sec: int) -> str:
    return f"2026-10-01T10:{sec // 60:02d}:{sec % 60:02d}Z"


def step(i, stype, source, **kw):
    return {
        "step_index": i,
        "type": stype,
        "source": source,
        "status": kw.pop("status", "DONE"),
        "created_at": ts(i),
        **kw,
    }


def call(name, **args):
    return {"name": name, "args": args}


def conv_1_steps() -> list[dict]:
    return [
        step(
            0,
            "USER_INPUT",
            "USER_EXPLICIT",
            content=f"hello, key api_key=abcdef123456 {FAKE_TOKEN}",
        ),
        step(1, "CONVERSATION_HISTORY", "SYSTEM"),
        step(2, "SYSTEM_MESSAGE", "SYSTEM", content="ephemeral system notice"),
        step(
            3,
            "PLANNER_RESPONSE",
            "MODEL",
            content="Let me look.",
            thinking="plan the zebra migration",
            tool_calls=[call("run_command", CommandLine="ls", Cwd="/tmp/proj")],
        ),
        step(4, "RUN_COMMAND", "MODEL", content="file1\nfile2"),
        step(
            5,
            "PLANNER_RESPONSE",
            "MODEL",
            tool_calls=[call("view_file", AbsolutePath="/tmp/proj/.env")],
        ),
        step(6, "VIEW_FILE", "MODEL", content="DB_URL=postgres://u:p@h/db\nPLAIN=value"),
        step(
            7,
            "PLANNER_RESPONSE",
            "MODEL",
            tool_calls=[call("view_file", AbsolutePath="/tmp/proj/big.log")],
        ),
        step(8, "VIEW_FILE", "MODEL", content=BIG),
        step(9, "RUN_COMMAND", "USER_EXPLICIT", content="user ran pwd"),
        step(10, "PLANNER_RESPONSE", "MODEL", status="RUNNING", content="mid-file stale step"),
        step(11, "PLANNER_RESPONSE", "MODEL", content="done"),
        step(12, "BRAND_NEW_STEP", "ROBOT", content="unknown source is counted, not fatal"),
    ]


def write_jsonl(path: Path, lines: list, *, raw_extra: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(x if isinstance(x, str) else json.dumps(x) for x in lines) + "\n"
    path.write_text(body + raw_extra, encoding="utf-8")
    return path


def transcript_path(root: Path, cid: str) -> Path:
    return root / "brain" / cid / ".system_generated" / "logs" / "transcript_full.jsonl"


def build_agy_root(root: Path) -> Path:
    """~/.gemini/antigravity-cli-like tree. Returns the root (the default source path)."""
    lines = conv_1_steps()
    lines.insert(4, "{not json at all")
    write_jsonl(
        transcript_path(root, CONV_1), lines, raw_extra='{"step_index": 99, "type": "USER_INP'
    )
    write_jsonl(
        transcript_path(root, CONV_2),
        [
            step(0, "USER_INPUT", "USER_EXPLICIT", content="zebra planning elsewhere"),
            step(1, "PLANNER_RESPONSE", "MODEL", content="ok"),
        ],
    )
    write_jsonl(
        transcript_path(root, CONV_LIVE),
        [
            step(0, "USER_INPUT", "USER_EXPLICIT", content="live conversation"),
            step(1, "PLANNER_RESPONSE", "MODEL", status="RUNNING", content="partial"),
        ],
    )
    # the truncated sibling must not be read
    write_jsonl(
        transcript_path(root, CONV_1).with_name("transcript.jsonl"),
        [step(0, "USER_INPUT", "USER_EXPLICIT", content=CANARY)],
    )
    # summaries DB (plain text columns) used for title / workspace / agent
    db = root / "conversation_summaries.db"
    c = sqlite3.connect(db)
    c.execute(
        "CREATE TABLE conversation_summaries (conversation_id TEXT, title TEXT, preview TEXT,"
        " step_count INTEGER, last_modified_time datetime, workspace_uris TEXT, status TEXT,"
        " agent_name TEXT, raw_summary BLOB)"
    )
    c.execute(
        "INSERT INTO conversation_summaries VALUES (?, 'Zebra migration', ?, 12, '2026-10-01',"
        " ?, '', 'research', ?)",
        (CONV_1, CANARY, json.dumps(["file:///tmp/proj"]), CANARY.encode()),
    )
    c.execute(
        "INSERT INTO conversation_summaries VALUES (?, NULL, NULL, 2, '2026-10-01', '[]', '',"
        " '', NULL)",
        (CONV_2,),
    )
    c.commit()
    c.close()
    # workspace -> latest conversation (project-linking fallback)
    (root / "cache").mkdir(parents=True, exist_ok=True)
    (root / "cache" / "last_conversations.json").write_text(
        json.dumps({"/tmp/proj/sub/deep": CONV_2, "/tmp/proj": CONV_1})
    )
    # opaque protobuf conversation DBs: never opened (random bytes + a canary)
    (root / "conversations").mkdir(parents=True, exist_ok=True)
    for cid in (CONV_1, CONV_OPAQUE):
        (root / "conversations" / f"{cid}.db").write_bytes(b"\x0a\x10" + CANARY.encode())
    (root / "conversations" / "eeeeeeee-5555-4555-8555-555555555555.pb").write_bytes(b"\x08\x01")
    # denylisted files: must never be read; planted with a canary
    (root / "history.jsonl").write_text(json.dumps({"cmd": CANARY}) + "\n")
    (root / "settings.json").write_text(json.dumps({"x": CANARY}))
    (root / "plugin_data" / "mem0").mkdir(parents=True, exist_ok=True)
    (root / "plugin_data" / "mem0" / "mcp_config.json").write_text(CANARY)
    return root
