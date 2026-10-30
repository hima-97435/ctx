"""Synthetic (hand-written, anonymized) Claude Code transcripts in the real JSONL shape."""

import json
from pathlib import Path

SESSION = "11111111-1111-4111-8111-111111111111"
SESSION_2 = "22222222-2222-4222-8222-222222222222"
FAKE_TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"  # fake, shape-only
CANARY = "CANARY-SUPERSECRET"
BIG = "A" * 70_000 + " token=deepsecretvalue123 " + "B" * 70_000


def _base(uuid, parent, ts, session=SESSION, cwd="/tmp/proj", **extra):
    return {
        "parentUuid": parent,
        "isSidechain": False,
        "uuid": uuid,
        "timestamp": ts,
        "userType": "external",
        "cwd": cwd,
        "sessionId": session,
        "version": "2.1.0",
        "gitBranch": "main",
        **extra,
    }


def user_text(uuid, parent, ts, text, **kw):
    return {
        **_base(uuid, parent, ts, **kw),
        "type": "user",
        "message": {"role": "user", "content": text},
    }


def assistant(uuid, parent, ts, blocks, model="claude-test-1", **kw):
    return {
        **_base(uuid, parent, ts, **kw),
        "type": "assistant",
        "message": {"id": "msg_x", "role": "assistant", "model": model, "content": blocks},
    }


def tool_result(uuid, parent, ts, call_id, content, **kw):
    return {
        **_base(uuid, parent, ts, **kw),
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": call_id, "content": content}],
        },
        "toolUseResult": {"stdout": "dup"},
    }


def main_lines() -> list[dict]:
    return [
        {"type": "mode", "mode": "normal", "sessionId": SESSION},
        user_text(
            "u1",
            None,
            "2026-10-01T10:00:00.000Z",
            f"hello, key is api_key=abcdef123456 {FAKE_TOKEN}",
        ),
        assistant(
            "a1",
            "u1",
            "2026-10-01T10:00:05.000Z",
            [{"type": "thinking", "thinking": "plan the zebra migration", "signature": "sig"}],
        ),
        assistant(
            "a2",
            "a1",
            "2026-10-01T10:00:06.000Z",
            [
                {"type": "text", "text": "Let me look."},
                {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "ls"}},
            ],
        ),
        tool_result("u2", "a2", "2026-10-01T10:00:07.000Z", "toolu_1", "file1\nfile2"),
        assistant(
            "a3",
            "u2",
            "2026-10-01T10:00:08.000Z",
            [
                {
                    "type": "tool_use",
                    "id": "toolu_2",
                    "name": "Read",
                    "input": {"file_path": "/tmp/proj/.env"},
                }
            ],
        ),
        tool_result(
            "u3",
            "a3",
            "2026-10-01T10:00:09.000Z",
            "toolu_2",
            [{"type": "text", "text": "DB_URL=postgres://u:p@h/db\nPLAIN=value"}],
        ),
        assistant(
            "a4",
            "u3",
            "2026-10-01T10:00:10.000Z",
            [
                {
                    "type": "tool_use",
                    "id": "toolu_3",
                    "name": "Read",
                    "input": {"file_path": "/tmp/proj/big.log"},
                }
            ],
        ),
        tool_result("u4", "a4", "2026-10-01T10:00:11.000Z", "toolu_3", BIG),
        {"type": "ai-title", "aiTitle": "Zebra migration", "sessionId": SESSION},
        {"type": "summary", "summary": "old summary", "leafUuid": "zzz"},
        {"type": "totally-new-type", "sessionId": SESSION, "x": 1},
        assistant(
            "s1",
            "a4",
            "2026-10-01T10:00:12.000Z",
            [{"type": "text", "text": "side"}],
            isSidechain=True,
        ),
        user_text(
            "m1",
            "a4",
            "2026-10-01T10:00:13.000Z",
            "<local-command-caveat>meta</local-command-caveat>",
            isMeta=True,
        ),
        assistant(
            "a5",
            "m1",
            "2026-10-01T10:00:14.000Z",
            [{"type": "text", "text": "done"}, {"type": "mystery_block", "x": 1}],
        ),
        user_text("u5", "a5", "2026-10-01T10:00:15.000Z", [{"type": "text", "text": "thanks"}]),
    ]


def second_session_lines() -> list[dict]:
    return [
        user_text(
            "v1",
            None,
            "2026-10-02T09:00:00.000Z",
            "zebra planning elsewhere",
            session=SESSION_2,
            cwd="/tmp/proj/sub/deep",
        ),
        assistant(
            "v2",
            "v1",
            "2026-10-02T09:00:01.000Z",
            [{"type": "text", "text": "ok"}],
            session=SESSION_2,
            cwd="/tmp/proj/sub/deep",
        ),
    ]


def write_jsonl(path: Path, lines: list, *, raw_extra: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(x if isinstance(x, str) else json.dumps(x) for x in lines) + "\n"
    path.write_text(body + raw_extra, encoding="utf-8")
    return path


def build_claude_root(root: Path) -> Path:
    """~/.claude-like tree. Returns the `projects` dir (the default source path)."""
    projects = root / "projects"
    lines = main_lines()
    # a malformed line and a truncated final line (live write) must not crash a sync
    lines.insert(3, "{not json at all")
    write_jsonl(
        projects / "-tmp-proj" / f"{SESSION}.jsonl", lines, raw_extra='{"type": "user", "uuid"'
    )
    write_jsonl(projects / "-tmp-proj-sub" / f"{SESSION_2}.jsonl", second_session_lines())
    # denylisted files: must never be read; planted with a canary
    (root / ".credentials.json").write_text(json.dumps({"token": CANARY}))
    (root / "settings.json").write_text(json.dumps({"env": {"X": CANARY}}))
    (projects / "-tmp-proj" / "settings.json").write_text(CANARY)
    return projects
