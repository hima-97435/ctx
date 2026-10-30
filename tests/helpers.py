"""Shared test helpers: a project config, an extended OpenCode fixture, a fake connector."""

import json
import sqlite3
from pathlib import Path

from ctx.connectors.opencode import OpenCodeConnector
from ctx.core.model import RawEvent, RawSession
from tests.fixtures.opencode_db import build_opencode_db

PROJECTS_TOML = """
[projects.proj]
name = "Proj"
allowed = true
roots = ["/tmp/proj"]

[projects.sub]
name = "Sub Project"
allowed = true
roots = ["/tmp/proj/sub"]

[projects.hidden]
name = "Hidden"
allowed = false
roots = ["/tmp/hidden"]
"""


def make_opencode_root(root: Path) -> OpenCodeConnector:
    """Fixture DB (ses_1 in /tmp/proj) plus ses_2 (/tmp/proj/sub/deep) and ses_3 (/tmp/hidden)."""
    root.mkdir(parents=True, exist_ok=True)
    db = build_opencode_db(root / "opencode.db")
    conn = sqlite3.connect(db)
    model = json.dumps({"id": "m", "providerID": "p"})
    for sid, directory, ts in (
        ("ses_2", "/tmp/proj/sub/deep", 5000),
        ("ses_3", "/tmp/hidden", 6000),
        ("ses_4", "/somewhere/else", 7000),
    ):
        conn.execute(
            "INSERT INTO session VALUES (?, 'p1', ?, 'T', '1', 'build', ?, ?, ?)",
            (sid, directory, model, ts, ts),
        )
        mid = f"msg_{sid}"
        conn.execute(
            "INSERT INTO message VALUES (?, ?, ?, ?, ?)", (mid, sid, ts, ts, '{"role":"user"}')
        )
        conn.execute(
            "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
            (
                f"prt_{sid}",
                mid,
                sid,
                ts,
                ts,
                json.dumps({"type": "text", "text": f"zebra migration plan in {sid}"}),
            ),
        )
    conn.commit()
    conn.close()
    return OpenCodeConnector(source_root=root)


class FakeConnector:
    """Source-agnostic stand-in that emits UNREDACTED text, to prove engine-side defenses."""

    name = "fake"
    schema_version_pin = "fake-v1"
    denylist_paths = ("secret.json",)
    denylist_tables = frozenset({"vault"})

    def __init__(self, root: Path, events: list[RawEvent] | None = None) -> None:
        self.root = root
        self.allowed_paths = [root]
        self.events = events if events is not None else []

    def default_source_path(self) -> Path:
        return self.root

    def check_version(self, source_path: Path) -> str:
        return "fake-1"

    def fetch(self, source_path, *, blob_store, tmp_dir, state=None):
        yield RawSession("fake", "s1", 1000, None, "/tmp/proj", None, None, "fake session")
        yield from self.events


def raw_event(source_id: str, text: str, **kw) -> RawEvent:
    base = {
        "source": "fake",
        "source_id": source_id,
        "ts": 1000,
        "session_ref": "s1",
        "role": "user",
        "kind": "text",
        "call_id": None,
        "synthetic": False,
        "text": text,
        "token_estimate": len(text) // 4,
        "truncated": False,
        "blob_ref": None,
        "raw_json": None,
        "content_hash": "0" * 64,
    }
    base.update(kw)
    return RawEvent(**base)
