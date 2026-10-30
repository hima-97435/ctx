"""Synthetic (hand-written, anonymized) Codex rollout JSONL in the documented shape."""

import json
from pathlib import Path

SID = "019aaaaa-0000-7000-8000-000000000001"
SID_2 = "019aaaaa-0000-7000-8000-000000000002"
CANARY = "CANARY-SUPERSECRET"
BIG = "A" * 70_000 + " token=deepsecretvalue123 " + "B" * 70_000


def rec(ts, type_, payload):
    return {"timestamp": ts, "type": type_, "payload": payload}


def msg(role, text, kind="input_text"):
    return {"type": "message", "role": role, "content": [{"type": kind, "text": text}]}


def rollout_lines(sid=SID, cwd="/tmp/proj", with_extras=True) -> list:
    lines = [
        rec(
            "2026-10-01T10:00:00.000Z",
            "session_meta",
            {
                "id": sid,
                "timestamp": "2026-10-01T10:00:00.000Z",
                "cwd": cwd,
                "originator": "codex_cli_rs",
                "cli_version": "0.99.0",
                "instructions": "BASE INSTRUCTIONS NOT STORED",
            },
        ),
        rec(
            "2026-10-01T10:00:01.000Z",
            "response_item",
            msg("user", "<environment_context>cwd=/tmp/proj</environment_context>"),
        ),
        rec("2026-10-01T10:00:02.000Z", "turn_context", {"cwd": cwd, "model": "gpt-test-1"}),
        rec(
            "2026-10-01T10:00:03.000Z",
            "response_item",
            msg("user", "hello api_key=abcdef123456 zebra"),
        ),
        rec("2026-10-01T10:00:04.000Z", "event_msg", {"type": "user_message", "message": "dup"}),
        rec(
            "2026-10-01T10:00:05.000Z",
            "response_item",
            {
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": "thinking it through"}],
                "encrypted_content": "gAAAA",
            },
        ),
        rec(
            "2026-10-01T10:00:06.000Z",
            "response_item",
            {
                "type": "function_call",
                "name": "shell",
                "call_id": "call_1",
                "arguments": json.dumps({"command": ["bash", "-lc", "ls"]}),
            },
        ),
        rec(
            "2026-10-01T10:00:07.000Z",
            "response_item",
            {"type": "function_call_output", "call_id": "call_1", "output": "file1\nfile2"},
        ),
        rec(
            "2026-10-01T10:00:08.000Z",
            "response_item",
            {
                "type": "function_call",
                "name": "shell",
                "call_id": "call_2",
                "arguments": json.dumps({"command": ["bash", "-lc", "cat .env"]}),
            },
        ),
        rec(
            "2026-10-01T10:00:09.000Z",
            "response_item",
            {
                "type": "function_call_output",
                "call_id": "call_2",
                "output": {"content": "DB_URL=postgres://u:p@h/db\nPLAIN=value", "success": True},
            },
        ),
        rec(
            "2026-10-01T10:00:10.000Z",
            "response_item",
            {
                "type": "function_call",
                "name": "shell",
                "call_id": "call_3",
                "arguments": json.dumps({"command": ["cat", "big.log"]}),
            },
        ),
        rec(
            "2026-10-01T10:00:11.000Z",
            "response_item",
            {"type": "function_call_output", "call_id": "call_3", "output": BIG},
        ),
        rec(
            "2026-10-01T10:00:12.000Z", "response_item", msg("assistant", "all done", "output_text")
        ),
    ]
    if with_extras:
        lines += [
            rec("2026-10-01T10:00:13.000Z", "response_item", {"type": "brand_new_item", "x": 1}),
            rec("2026-10-01T10:00:14.000Z", "future_record_type", {"x": 1}),
            rec("2026-10-01T10:00:15.000Z", "response_item", msg("developer", "dev note")),
        ]
    return lines


def write_rollout(path: Path, lines: list, raw_extra: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(x if isinstance(x, str) else json.dumps(x) for x in lines) + "\n"
    path.write_text(body + raw_extra, encoding="utf-8")
    return path


def build_codex_root(root: Path) -> Path:
    """~/.codex-like tree. Returns the `sessions` dir (the default source path)."""
    sessions = root / "sessions"
    lines = rollout_lines()
    lines.insert(4, "{broken json")
    write_rollout(
        sessions / "2026/10/01" / f"rollout-2026-10-01T10-00-00-{SID}.jsonl",
        lines,
        raw_extra='{"type": "response_item", "payl',
    )
    second = rollout_lines(SID_2, "/tmp/proj/sub/deep", with_extras=False)[:4]
    write_rollout(sessions / "2026/10/02" / f"rollout-2026-10-02T09-00-00-{SID_2}.jsonl", second)
    (root / "auth.json").write_text(json.dumps({"OPENAI_API_KEY": CANARY}))
    (root / "config.toml").write_text(f'token = "{CANARY}"')
    (sessions / "auth.json").write_text(CANARY)
    return sessions
