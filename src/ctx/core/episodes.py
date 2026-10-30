"""Deterministic episodes and the timeline built on them (M2, ADR-026). No LLM (rule 7).

An episode is a maximal run of one project's events (from ALL sources, merged by
timestamp) in which consecutive events are less than `gap` apart. A gap of exactly
`gap` starts a new episode. Events with no project form one "unlinked" bucket.
Episodes are derived: `rebuild_episodes` recomputes them from `events` + `sessions`
alone, and ids are assigned in a canonical order so the same events give the same ids.

The timeline reads episodes, splits each at local midnight, and summarizes per
(local day, episode): counts, sessions with titles, and top files / commands
derived source-agnostically from `tool:*` event text.
"""

import json
import os
import re
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ctx.core.redact import redact

DEFAULT_GAP_MINUTES = 45
UNLINKED = "(unlinked)"
TOP_N = 5
_FILE_KEYS = ("file_path", "filePath", "path", "filename", "notebook_path", "file")
_COMMAND_KEYS = ("command", "cmd")
_MAX_CMD_CHARS = 80
_MAX_PATH_CHARS = 120


# --------------------------------------------------------------------------- rebuild


def episode_gap_minutes() -> int:
    """Idle gap in minutes: `CTX_EPISODE_GAP_MINUTES`, else 45."""
    raw = os.environ.get("CTX_EPISODE_GAP_MINUTES")
    if raw is None or not raw.strip():
        return DEFAULT_GAP_MINUTES
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"CTX_EPISODE_GAP_MINUTES must be an integer, got {raw!r}") from exc
    if value < 1:
        raise ValueError("CTX_EPISODE_GAP_MINUTES must be >= 1")
    return value


def segment(rows: list[tuple[int, int]], gap_ms: int) -> list[list[tuple[int, int]]]:
    """Split (ts, event_id) pairs, already sorted by (ts, id), at idle gaps >= gap_ms."""
    runs: list[list[tuple[int, int]]] = []
    for ts, event_id in rows:
        if runs and ts - runs[-1][-1][0] < gap_ms:
            runs[-1].append((ts, event_id))
        else:
            runs.append([(ts, event_id)])
    return runs


def rebuild_episodes(conn: sqlite3.Connection, gap_minutes: int | None = None) -> int:
    """Recompute every episode from `events` alone; returns the number of episodes.

    Deletes and recreates only the derived `episodes`/`episode_events` rows. Ids are
    1..n in (start_ts, project_id, first event id) order, so a rebuild with unchanged
    events yields identical ids.
    """
    gap = gap_minutes if gap_minutes is not None else episode_gap_minutes()
    if gap < 1:
        raise ValueError("episode gap must be >= 1 minute")
    gap_ms = gap * 60_000
    rows = conn.execute(
        "SELECT e.id AS id, e.ts AS ts, COALESCE(s.project_id, e.project_id) AS pid"
        " FROM events e LEFT JOIN sessions s ON s.id = e.session_id"
    ).fetchall()
    by_project: dict[str | None, list[tuple[int, int]]] = {}
    for r in rows:
        by_project.setdefault(r["pid"], []).append((r["ts"], r["id"]))
    episodes: list[tuple[int, str, int, str | None, list[tuple[int, int]]]] = []
    for pid, pairs in by_project.items():
        pairs.sort()
        for run in segment(pairs, gap_ms):
            episodes.append((run[0][0], pid or "", run[0][1], pid, run))
    episodes.sort(key=lambda x: x[:3])
    now = datetime.now(UTC).isoformat(timespec="seconds")
    conn.execute("BEGIN")
    try:
        conn.execute("DELETE FROM episode_events")
        conn.execute("DELETE FROM episodes")
        for n, (_, _, _, pid, run) in enumerate(episodes, 1):
            conn.execute(
                "INSERT INTO episodes(id, project_id, start_ts, end_ts, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (n, pid, run[0][0], run[-1][0], now),
            )
            conn.executemany(
                "INSERT INTO episode_events(episode_id, event_id) VALUES (?, ?)",
                [(n, event_id) for _, event_id in run],
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return len(episodes)


# --------------------------------------------------------------------------- time zones


def _system_zone() -> tzinfo:
    """The machine's zone with DST rules when discoverable, else its current fixed offset."""
    name = os.environ.get("TZ", "").lstrip(":")
    candidates = [name] if name else []
    try:
        target = str(Path("/etc/localtime").resolve())
        if "zoneinfo/" in target:
            candidates.append(target.split("zoneinfo/", 1)[1])
    except OSError:
        pass
    for key in candidates:
        try:
            return ZoneInfo(key)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            continue
    return datetime.fromtimestamp(time.time()).astimezone().tzinfo or UTC


def resolve_tz(name: str | None = None) -> tzinfo:
    """`name` (IANA, e.g. Europe/Berlin or UTC), else $CTX_TZ, else the system zone."""
    name = name or os.environ.get("CTX_TZ") or None
    if name is None:
        return _system_zone()
    if name.upper() == "UTC":
        return UTC
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise ValueError(
            f"unknown time zone {name!r}; use an IANA name like Europe/Berlin"
        ) from exc


def local_day_bounds(day: date, tz: tzinfo) -> tuple[int, int]:
    """[start, end] epoch ms of a local calendar day (end = last ms). DST-correct."""
    start = datetime(day.year, day.month, day.day, tzinfo=tz)
    nxt = day + timedelta(days=1)
    end = datetime(nxt.year, nxt.month, nxt.day, tzinfo=tz)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000) - 1


def parse_day(value: str, tz: tzinfo, *, now: datetime | None = None) -> date:
    """'today', 'yesterday' or YYYY-MM-DD, as a calendar date in `tz`."""
    v = value.strip().lower()
    today = (now or datetime.now(UTC)).astimezone(tz).date()
    if v == "today":
        return today
    if v == "yesterday":
        return today - timedelta(days=1)
    try:
        return date.fromisoformat(v)
    except ValueError as exc:
        raise ValueError(f"cannot parse day {value!r}: use today, yesterday or YYYY-MM-DD") from exc


_BARE_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_RELATIVE = re.compile(r"\d+[smhdw]")


def parse_local(
    value: str, tz: tzinfo, *, end_of_day: bool = False, now: datetime | None = None
) -> int:
    """Like search.parse_when, but bare dates and naive datetimes are in `tz`, not UTC."""
    from ctx.core.search import parse_when

    v = value.strip()
    if _RELATIVE.fullmatch(v):
        return parse_when(v, now=now)
    if _BARE_DATE.fullmatch(v):
        start, end = local_day_bounds(date.fromisoformat(v), tz)
        return end if end_of_day else start
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return parse_when(v, now=now)  # raises the standard message
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return int(dt.timestamp() * 1000)


# --------------------------------------------------------------------------- timeline


@dataclass(frozen=True)
class SessionRef:
    source: str
    session_id: str
    title: str | None
    messages: int


@dataclass
class TimelineEntry:
    day: str  # local YYYY-MM-DD
    episode_id: int
    project_id: str | None
    project_name: str
    start_ts: int  # first matching event in this day, epoch ms
    end_ts: int
    continues_prev: bool  # the episode began before this local day
    continues_next: bool  # the episode runs past this local day
    events: int = 0
    messages: int = 0  # kind == 'text'
    tool_calls: int = 0  # kind startswith 'tool:'
    sources: list[str] = field(default_factory=list)
    sessions: list[SessionRef] = field(default_factory=list)
    top_files: list[tuple[str, int]] = field(default_factory=list)
    top_commands: list[tuple[str, int]] = field(default_factory=list)


def _tool_arguments(text: str) -> dict | None:
    """Parse the `input: {json}` form tool events use; None if absent or truncated."""
    if not text.startswith("input:"):
        return None
    try:
        data = json.loads(text[6:].strip())
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def tool_targets(text: str) -> tuple[list[str], list[str]]:
    """Best-effort (files, commands) named by one tool event, source-agnostic."""
    args = _tool_arguments(text)
    if args is None:
        return [], []
    files = [args[k] for k in _FILE_KEYS if isinstance(args.get(k), str) and args[k]]
    commands: list[str] = []
    for k in _COMMAND_KEYS:
        v = args.get(k)
        if isinstance(v, list):
            v = " ".join(str(x) for x in v)
        if isinstance(v, str) and v.strip():
            commands.append(v)
    return files, commands


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _top(counter: Counter, n: int = TOP_N) -> list[tuple[str, int]]:
    return sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:n]


def build_timeline(
    conn: sqlite3.Connection,
    tz: tzinfo,
    *,
    project: str | None = None,
    source: str | None = None,
    since: int | None = None,
    until: int | None = None,
    include_all: bool = False,
    exclude_mcp: bool = False,
    limit: int | None = None,
) -> list[TimelineEntry]:
    """Per (local day, episode) summaries, newest day first, chronological within a day.

    Without `include_all` only `allowed=1` projects are shown (unlinked events are
    hidden); `exclude_mcp` additionally drops `mcp_exclude=1` projects. `limit` keeps
    the most recent N entries.
    """
    from ctx.core.search import resolve_project

    where = ["1=1"]
    params: list[object] = []
    if project is not None:
        where.append("ep.project_id = ?")
        params.append(resolve_project(conn, project))
    if not include_all:
        where.append("p.allowed = 1")
    if exclude_mcp:
        where.append("p.mcp_exclude = 0")
    if source is not None:
        where.append("e.source = ?")
        params.append(source)
    if since is not None:
        where.append("e.ts >= ?")
        params.append(since)
    if until is not None:
        where.append("e.ts <= ?")
        params.append(until)
    rows = conn.execute(
        "SELECT ep.id AS eid, ep.project_id AS pid, p.name AS pname, ep.start_ts AS estart,"
        " ep.end_ts AS eend, e.ts, e.source, e.kind, e.session_id, s.source_session_id AS ssid,"
        " s.title AS title,"
        " CASE WHEN e.kind LIKE 'tool:%' THEN e.text END AS tool_text"
        " FROM episode_events ee JOIN episodes ep ON ep.id = ee.episode_id"
        " JOIN events e ON e.id = ee.event_id"
        " LEFT JOIN projects p ON p.id = ep.project_id"
        " LEFT JOIN sessions s ON s.id = e.session_id"
        f" WHERE {' AND '.join(where)} ORDER BY ep.id, e.ts, e.id",
        params,
    ).fetchall()

    entries: dict[tuple[str, int], TimelineEntry] = {}
    session_counts: dict[tuple[str, int], dict[tuple, list]] = {}
    files: dict[tuple[str, int], Counter] = {}
    commands: dict[tuple[str, int], Counter] = {}
    for r in rows:
        day = datetime.fromtimestamp(r["ts"] / 1000, tz).date()
        key = (day.isoformat(), r["eid"])
        entry = entries.get(key)
        if entry is None:
            day_start, day_end = local_day_bounds(day, tz)
            entry = entries[key] = TimelineEntry(
                day=key[0],
                episode_id=r["eid"],
                project_id=r["pid"],
                project_name=r["pname"] or UNLINKED,
                start_ts=r["ts"],
                end_ts=r["ts"],
                continues_prev=r["estart"] < day_start,
                continues_next=r["eend"] > day_end,
            )
            session_counts[key] = {}
            files[key] = Counter()
            commands[key] = Counter()
        entry.end_ts = r["ts"]
        entry.events += 1
        if r["kind"] == "text":
            entry.messages += 1
        sess = session_counts[key].setdefault(
            (r["source"], r["ssid"] or r["session_id"] or "-"), [r["title"], 0]
        )
        if r["kind"] == "text":
            sess[1] += 1
        if r["source"] not in entry.sources:
            entry.sources.append(r["source"])
        if r["tool_text"] is not None:
            entry.tool_calls += 1
            fs, cs = tool_targets(r["tool_text"])
            files[key].update(_clip(redact(f), _MAX_PATH_CHARS) for f in fs)
            commands[key].update(
                _clip(redact(c.strip().splitlines()[0]), _MAX_CMD_CHARS) for c in cs if c.strip()
            )
    for key, entry in entries.items():
        entry.sources.sort()
        entry.sessions = [
            SessionRef(src, sid, redact(title) if title else None, n)
            for (src, sid), (title, n) in sorted(session_counts[key].items(), key=_session_order)
        ]
        entry.top_files = _top(files[key])
        entry.top_commands = _top(commands[key])
    ordered = sorted(entries.values(), key=lambda x: (x.start_ts, x.episode_id), reverse=True)
    if limit is not None:
        ordered = ordered[: max(limit, 0)]
    return _by_day_desc(ordered)


def _session_order(item: tuple[tuple[str, str], list]) -> tuple:
    (src, sid), (_, n) = item
    return (-n, src, sid)


def _by_day_desc(entries: list[TimelineEntry]) -> list[TimelineEntry]:
    """Days newest first; within a day, chronological."""
    return sorted(entries, key=lambda x: (_neg_day(x.day), x.start_ts, x.episode_id))


def _neg_day(day: str) -> int:
    return -date.fromisoformat(day).toordinal()


def _hm(ms: int, tz: tzinfo) -> str:
    return datetime.fromtimestamp(ms / 1000, tz).strftime("%H:%M")


def render_timeline(entries: list[TimelineEntry], tz: tzinfo, *, max_titles: int = 5) -> str:
    """Human-readable timeline grouped by day then project. Output is re-redacted."""
    if not entries:
        return "no activity"
    lines: list[str] = []
    current_day = None
    for e in entries:
        if e.day != current_day:
            current_day = e.day
            weekday = date.fromisoformat(e.day).strftime("%a")
            lines.append(f"{e.day} {weekday} ({_zone_label(e, tz)})")
        span = f"{_hm(e.start_ts, tz)}-{_hm(e.end_ts, tz)}"
        flags = ("<" if e.continues_prev else "") + (">" if e.continues_next else "")
        lines.append(
            f"  {e.project_name} [{span}{' ' + flags if flags else ''}] "
            f"episode={e.episode_id} sources={','.join(e.sources)} "
            f"messages={e.messages} tools={e.tool_calls} events={e.events}"
        )
        for s in e.sessions[:max_titles]:
            lines.append(f"    - {s.source}: {s.title or '(untitled)'} ({s.messages} msgs)")
        if len(e.sessions) > max_titles:
            lines.append(f"    - ... {len(e.sessions) - max_titles} more sessions")
        if e.top_files:
            lines.append("    files: " + ", ".join(f"{p} x{n}" for p, n in e.top_files))
        if e.top_commands:
            lines.append("    commands: " + ", ".join(f"{c} x{n}" for c, n in e.top_commands))
    return redact("\n".join(lines))


def _zone_label(e: TimelineEntry, tz: tzinfo) -> str:
    return datetime.fromtimestamp(e.start_ts / 1000, tz).strftime("%Z") or str(tz)
