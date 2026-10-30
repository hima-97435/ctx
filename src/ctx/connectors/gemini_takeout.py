"""Gemini Apps connector over Google Takeout (source name: "gemini").

Reads "My Activity > Gemini Apps" from a Takeout zip or extracted folder:
`.../Gemini Apps/MyActivity.json` (preferred) or `MyActivity.html` (fallback, parsed
with the stdlib `html.parser`; English UI only). Other My Activity products (Search,
YouTube, ...) and all other Takeout data are never read.

Every activity is one prompt (+ optional response) at one time. Takeout has no
conversation ids, so activities are grouped into sessions deterministically: sorted by
time, a new session starts when the gap to the previous activity exceeds 30 minutes
(`SESSION_GAP_MS`). Session id = `g<first activity epoch ms>`; event ids derive from
time + prompt (`<activity id>:prompt|response`). A later superset export may merge two
earlier sessions; already-stored events keep their old session (ADR-025).
"""

import hashlib
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any

from ctx.connectors.base import ConnectorError
from ctx.connectors.jsonl_common import IngestStats, build_event, parse_ts
from ctx.connectors.webexport_common import ExportArchive, WebExportConnector, load_json
from ctx.core.blobs import BlobStore
from ctx.core.model import RawEvent, RawSession
from ctx.core.redact import redact

SOURCE = "gemini"
SCHEMA_VERSION_PIN = "gemini-takeout-v1"
DENYLIST_PATHS = (
    "*account*.json",
    "*auth*.json",
    "archive_browser.html",
    ".env",
)
SESSION_GAP_MS = 30 * 60 * 1000
_GEMINI_DIRS = frozenset({"gemini apps", "gemini", "bard"})
_GEMINI_HEADERS = frozenset({"gemini apps", "gemini", "bard"})
_MONTHS = {
    m: i
    for i, m in enumerate(
        ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1
    )
}
_TS_RE = re.compile(
    r"([A-Z][a-z]{2}) (\d{1,2}), (\d{4}),? (\d{1,2}):(\d{2}):(\d{2})\s*([AP]M)\s*(\S*)"
)
_TZ_RE = re.compile(r"^(?:UTC|GMT)(?:([+-])(\d{1,2})(?::?(\d{2}))?)?$")
_BLOCK_TAGS = frozenset(
    {"p", "br", "div", "li", "ul", "ol", "tr", "table", "pre", "blockquote"}
    | {f"h{i}" for i in range(1, 7)}
)


def _norm(text: str) -> str:
    return text.replace(" ", " ").replace(" ", " ").replace(" ", " ")


class _TextExtractor(HTMLParser):
    """HTML fragment -> plain text (block tags become newlines, <li> become bullets)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    lines = [ln.strip() for ln in _norm("".join(parser.parts)).splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


class _ActivityHtml(HTMLParser):
    """Collects the text of each Takeout activity body cell (`content-cell ... body-1`)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.cells: list[str] = []
        self._depth = 0  # >0 while inside a body cell (counts nested divs)
        self._buf: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if self._depth:
            if tag == "div":
                self._depth += 1
            if tag in ("script", "style"):
                self._skip += 1
            elif tag == "li":
                self._buf.append("\n- ")
            elif tag in _BLOCK_TAGS:
                self._buf.append("\n")
            return
        classes = (dict(attrs).get("class") or "").split()
        if (
            tag == "div"
            and "content-cell" in classes
            and "mdl-cell--6-col" in classes
            and "mdl-typography--body-1" in classes
            and "mdl-typography--text-right" not in classes
        ):
            self._depth, self._buf = 1, []

    def handle_endtag(self, tag: str) -> None:
        if not self._depth:
            return
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS and tag != "div":
            self._buf.append("\n")
        if tag == "div":
            self._depth -= 1
            if self._depth == 0:
                self.cells.append("".join(self._buf))
            else:
                self._buf.append("\n")

    def handle_data(self, data: str) -> None:
        if self._depth and not self._skip:
            self._buf.append(data)


def _parse_html_time(line: str, stats: IngestStats) -> int | None:
    m = _TS_RE.search(line)
    if not m:
        return None
    mon, day, year, hour, minute, sec, ampm, tzname = m.groups()
    h = int(hour) % 12 + (12 if ampm == "PM" else 0)
    tzm = _TZ_RE.match(tzname or "UTC")
    offset = timedelta(0)
    if tzm:
        if tzm.group(1):
            offset = timedelta(hours=int(tzm.group(2)), minutes=int(tzm.group(3) or 0))
            offset = -offset if tzm.group(1) == "-" else offset
    else:
        stats.warn(f"unrecognized time zone {tzname!r} in MyActivity.html treated as UTC")
    local = datetime(
        int(year), _MONTHS[mon], int(day), h, int(minute), int(sec), tzinfo=timezone(offset)
    )
    return int(local.astimezone(UTC).timestamp() * 1000)


def _html_activities(text: str, stats: IngestStats) -> list[tuple[int, str, str]]:
    parser = _ActivityHtml()
    parser.feed(text)
    parser.close()
    out: list[tuple[int, str, str]] = []
    for cell in parser.cells:
        lines = [ln.strip() for ln in _norm(cell).splitlines()]
        lines = [ln for ln in lines if ln]
        idx = next((i for i, ln in enumerate(lines) if _TS_RE.search(ln)), None)
        if not lines or not lines[0].lower().startswith("prompted") or idx is None:
            stats.skipped["non_prompt_activity"] += 1
            continue
        ts = _parse_html_time(lines[idx], stats)
        prompt = "\n".join(lines[:idx])[len("prompted") :].strip()
        if ts is None or not prompt:
            stats.skipped["malformed_activity"] += 1
            continue
        out.append((ts, prompt, "\n".join(lines[idx + 1 :]).strip()))
    return out


def _json_activities(data: list, stats: IngestStats) -> list[tuple[int, str, str]]:
    out: list[tuple[int, str, str]] = []
    for entry in data:
        if not isinstance(entry, dict):
            stats.skipped["malformed_activity"] += 1
            continue
        header = entry.get("header")
        products = entry.get("products")
        labels = {str(x).lower() for x in (products if isinstance(products, list) else [])}
        if isinstance(header, str):
            labels.add(header.lower())
        if labels and not labels & _GEMINI_HEADERS:
            stats.skipped["non_gemini_activity"] += 1
            continue
        title = _norm(entry.get("title") if isinstance(entry.get("title"), str) else "")
        ts = parse_ts(entry.get("time"))
        if not title.lower().startswith("prompted"):
            stats.skipped["non_prompt_activity"] += 1
            continue
        prompt = title[len("prompted") :].strip()
        if ts is None or not prompt:
            stats.skipped["malformed_activity"] += 1
            continue
        items = entry.get("safeHtmlItem")
        response = "\n\n".join(
            html_to_text(i["html"])
            for i in (items if isinstance(items, list) else [])
            if isinstance(i, dict) and isinstance(i.get("html"), str)
        ).strip()
        out.append((ts, prompt, response))
    return out


def _is_gemini_member(name: str) -> bool:
    parts = [p.lower() for p in name.split("/")]
    if parts[-1] not in ("myactivity.json", "myactivity.html"):
        return False
    return len(parts) == 1 or any(p in _GEMINI_DIRS for p in parts[:-1])


class GeminiTakeoutConnector(WebExportConnector):
    name = SOURCE
    dirname = "gemini"
    schema_version_pin = SCHEMA_VERSION_PIN
    denylist_paths = DENYLIST_PATHS

    def select(self, names: list[str]) -> list[str]:
        found = sorted(n for n in names if _is_gemini_member(n))
        has_json = {n.rsplit("/", 1)[0] if "/" in n else "" for n in found if n.endswith(".json")}
        # JSON preferred: an HTML file is used only when its folder has no JSON
        return [
            n
            for n in found
            if n.endswith(".json") or (n.rsplit("/", 1)[0] if "/" in n else "") not in has_json
        ]

    def validated_members(self, members: list[str]) -> list[str]:
        return members

    def validate(self, member: str, data: Any) -> None:
        if member.endswith(".html"):
            if not isinstance(data, str) or "content-cell" not in data:
                raise ConnectorError(f"{member}: Takeout activity HTML not recognized")
            return
        if not isinstance(data, list):
            raise ConnectorError(f"{member}: MyActivity.json must be a JSON list of activities")
        head = [e for e in data[:20] if isinstance(e, dict)]
        if data and not any("title" in e and "time" in e for e in head):
            raise ConnectorError(
                f"{member}: Takeout activity format unrecognized (no 'title'/'time');"
                " failing loud, nothing was written"
            )

    def check_version(self, source_path):  # HTML members are text, not JSON
        archives = self._archives(source_path)
        for archive in archives:
            for member in self.select(archive.names()):
                self.validate(member, self._load(archive, member))
        return f"{self.schema_version_pin}:empty" if not archives else self.schema_version_pin

    def _load(self, archive: ExportArchive, member: str) -> Any:
        if member.endswith(".json"):
            return load_json(archive, member, self.denylist_paths)
        return archive.read(member, self.denylist_paths).decode("utf-8", errors="replace")

    def conversations(
        self,
        archive: ExportArchive,
        members: list[str],
        blob_store: BlobStore,
        stats: IngestStats,
    ) -> Iterator[tuple[RawSession, list[RawEvent]]]:
        activities: list[tuple[int, str, str]] = []
        for member in members:
            data = self._load(archive, member)
            activities += (
                _json_activities(data, stats)
                if member.endswith(".json")
                else _html_activities(data, stats)
            )
        activities.sort(key=lambda a: (a[0], a[1]))
        groups: list[list[tuple[int, str, str]]] = []
        for act in activities:
            if groups and act[0] - groups[-1][-1][0] <= SESSION_GAP_MS:
                groups[-1].append(act)
            else:
                groups.append([act])
        seen: set[str] = set()
        for group in groups:
            sid = f"g{group[0][0]}"
            events: list[RawEvent] = []
            for ts, prompt, response in group:
                aid = hashlib.sha256(f"{ts}|{prompt}".encode()).hexdigest()[:16]
                if aid in seen:
                    stats.skipped["duplicate_activity"] += 1
                    continue
                seen.add(aid)
                for suffix, role, text in (
                    ("prompt", "user", prompt),
                    ("response", "assistant", response),
                ):
                    if not text:
                        continue
                    events.append(
                        build_event(
                            source=SOURCE,
                            source_id=f"{aid}:{suffix}",
                            ts=ts,
                            session_ref=sid,
                            role=role,
                            kind="text",
                            call_id=None,
                            synthetic=False,
                            content=text,
                            hash_basis=f"{role}:{text}",
                            message_ref=aid,
                            blob_store=blob_store,
                            stats=stats,
                        )
                    )
            first_prompt = group[0][1].replace("\n", " ")
            yield (
                RawSession(
                    source=SOURCE,
                    source_session_id=sid,
                    started_at=group[0][0],
                    ended_at=group[-1][0],
                    cwd=None,
                    model=None,
                    agent=None,
                    title=redact(first_prompt[:80]),
                ),
                events,
            )
