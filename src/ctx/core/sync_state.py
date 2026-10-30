"""Per-source incremental-sync state (derived, rebuildable; never events).

Connectors may record cheap change cursors (a JSONL file's size+mtime, a SQLite
database's size+mtime incl. its -wal) so an unchanged unit is skipped on the next
sync. The engine owns persistence: values a connector `stage`s are written in the
same transaction as the events of that run, so a failed run records nothing.

The table can be dropped or emptied at any time: with no state every unit is
re-read and `UNIQUE(source, source_id)` keeps the result duplicate-free (rule 11).
"""

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def fingerprint(*paths: Path) -> list[list[int] | None]:
    """[[size, mtime_ns], ...] for each path (None when it does not exist).

    Take it *before* reading the file: if the file grows while it is read, the
    stored fingerprint is stale and the next sync simply re-reads it.
    """
    out: list[list[int] | None] = []
    for p in paths:
        try:
            st = Path(p).stat()
            out.append([st.st_size, st.st_mtime_ns])
        except OSError:
            out.append(None)
    return out


class SyncState:
    """Key/value cursors for one source. `SyncState()` (no db) is empty and inert."""

    def __init__(
        self, conn: sqlite3.Connection | None = None, source: str = "", *, use_saved: bool = True
    ) -> None:
        self.source = source
        self.unchanged = 0  # units (files / databases) skipped because their cursor matched
        self._saved: dict[str, Any] = {}
        self._staged: dict[str, Any] = {}
        if conn is not None and use_saved:
            for row in conn.execute(
                "SELECT key, value FROM sync_state WHERE source = ?", (source,)
            ):
                try:
                    self._saved[row["key"]] = json.loads(row["value"])
                except (TypeError, json.JSONDecodeError):
                    continue  # unreadable cursor == no cursor

    def get(self, key: str) -> Any | None:
        return self._saved.get(key)

    def is_unchanged(self, key: str, value: Any) -> bool:
        """True (and counted) when the saved cursor equals `value`."""
        if key in self._saved and self._saved[key] == json.loads(json.dumps(value)):
            self.unchanged += 1
            return True
        return False

    def stage(self, key: str, value: Any) -> None:
        self._staged[key] = value

    def flush(self, conn: sqlite3.Connection) -> None:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        for key, value in self._staged.items():
            conn.execute(
                "INSERT INTO sync_state(source, key, value, updated_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(source, key) DO UPDATE SET value=excluded.value,"
                " updated_at=excluded.updated_at",
                (self.source, key, json.dumps(value), now),
            )
        self._staged.clear()
