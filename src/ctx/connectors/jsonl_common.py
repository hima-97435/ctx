"""Source-neutral helpers shared by the JSONL transcript connectors.

Nothing here knows about a specific tool's record shapes: line reading (incl.
optional .zst), timestamp parsing, event construction (redact-then-spill,
ADR-012/017), per-run statistics and the generic dry-run report. Source-specific
mapping lives in claude_code.py / codex.py (ADR-007).
"""

import hashlib
import io
import json
import logging
import shlex
import tempfile
import time
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any

from ctx.connectors.base import Connector, ConnectorError
from ctx.core.blobs import BlobStore
from ctx.core.model import RawEvent
from ctx.core.redact import is_sensitive_file, redact_counted
from ctx.core.truncate import SPILL_THRESHOLD_CHARS, prepare_text

log = logging.getLogger(__name__)


@dataclass
class IngestStats:
    """What one fetch saw. Skips are counted by reason, never silent."""

    files: int = 0
    lines: int = 0
    malformed_lines: int = 0
    lines_by_type: Counter = field(default_factory=Counter)
    skipped: Counter = field(default_factory=Counter)
    events_by_kind: Counter = field(default_factory=Counter)
    roles: Counter = field(default_factory=Counter)
    redaction_matches_by_kind: Counter = field(default_factory=Counter)
    events_with_redaction: int = 0
    oversized_events: int = 0
    oversized_content_chars: int = 0
    warnings: list[str] = field(default_factory=list)

    def warn(self, message: str) -> None:
        log.warning(message)
        if message not in self.warnings:
            self.warnings.append(message)


def zstandard_module() -> Any | None:
    """The optional `zstandard` module, or None (never a hard dependency)."""
    try:
        import zstandard
    except ImportError:
        return None
    return zstandard


def open_text(path: Path) -> IO[str]:
    """Open a transcript for line reading; .zst only when zstandard is installed."""
    if path.name.endswith(".zst"):
        zstd = zstandard_module()
        if zstd is None:
            raise ConnectorError(f"{path.name}: .zst needs the optional 'zstandard' package")
        raw = path.open("rb")
        return io.TextIOWrapper(
            zstd.ZstdDecompressor().stream_reader(raw), encoding="utf-8", errors="replace"
        )
    return path.open("r", encoding="utf-8", errors="replace")


def read_records(path: Path, stats: IngestStats | None = None) -> Iterator[tuple[int, str, Any]]:
    """Yield (line_no, raw_line, parsed JSON) for every non-empty line.

    Unparsable lines (e.g. a half-written final line of a live transcript) yield
    parsed=None and are counted, never raised.
    """
    with open_text(path) as fh:
        for line_no, raw in enumerate(fh, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                parsed = None
                if stats is not None:
                    stats.malformed_lines += 1
            yield line_no, line, parsed


def parse_ts(value: Any) -> int | None:
    """ISO-8601 string (or epoch seconds/ms number) -> epoch milliseconds."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return int(value if value > 10_000_000_000 else value * 1000)
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)  # noqa: UP017 - explicit, matches ISO "Z"
    return int(dt.timestamp() * 1000)


def sensitive_path_in(values: Iterable[Any]) -> str | None:
    """First string (or shell-command token) naming a sensitive file such as .env."""
    for value in values:
        if not isinstance(value, str):
            continue
        if is_sensitive_file(value):
            return value
        try:
            tokens = shlex.split(value)
        except ValueError:
            tokens = value.split()
        for token in tokens:
            if is_sensitive_file(token):
                return token
    return None


def flatten_strings(value: Any, limit: int = 200) -> list[str]:
    """Strings found in a (possibly nested) tool input, for sensitive-path detection."""
    out: list[str] = []
    stack = [value]
    while stack and len(out) < limit:
        item = stack.pop()
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return out


def build_event(
    *,
    source: str,
    source_id: str,
    ts: int,
    session_ref: str,
    role: str | None,
    kind: str,
    call_id: str | None,
    synthetic: bool,
    content: str,
    hash_basis: str,
    message_ref: str | None,
    blob_store: BlobStore,
    stats: IngestStats,
    file_counts: dict[str, int] | None = None,
) -> RawEvent:
    """Redact first, then spill oversized content to a blob (redact-then-spill)."""
    redacted, counts = redact_counted(content)
    for k, n in (file_counts or {}).items():
        counts[k] = counts.get(k, 0) + n
    if counts:
        stats.events_with_redaction += 1
        for k, n in counts.items():
            stats.redaction_matches_by_kind[k] += n
    if len(content) > SPILL_THRESHOLD_CHARS:
        stats.oversized_events += 1
        stats.oversized_content_chars += len(content)
    prepared = prepare_text(redacted, blob_store=blob_store)
    stats.events_by_kind[kind] += 1
    if role:
        stats.roles[role] += 1
    return RawEvent(
        source=source,
        source_id=source_id,
        ts=ts,
        session_ref=session_ref,
        role=role,
        kind=kind,
        call_id=call_id,
        synthetic=synthetic,
        text=prepared.text,
        token_estimate=prepared.token_estimate,
        truncated=prepared.truncated,
        blob_ref=prepared.blob_ref,
        raw_json=None,
        content_hash=hashlib.sha256(hash_basis.encode("utf-8")).hexdigest(),
        message_ref=message_ref,
    )


def tool_input_text(tool_input: Any) -> str:
    return "input: " + json.dumps(tool_input, ensure_ascii=False, sort_keys=True)


@dataclass
class DryRun:
    source: str
    source_path: str
    schema_version: str
    sessions: int
    events: int
    elapsed_s: float
    stats: IngestStats


def dry_run_report(connector: Connector, source_path: Path | None = None) -> DryRun:
    """Read-only pass: runs the real fetch into a throwaway dir; writes nothing real."""
    path = Path(source_path) if source_path else connector.default_source_path()
    start = time.perf_counter()
    version = connector.check_version(path)
    sessions = events = 0
    with tempfile.TemporaryDirectory(prefix="ctx-dry-run-") as tmp:
        for record in connector.fetch(
            path, blob_store=BlobStore(Path(tmp)), tmp_dir=Path(tmp) / "t"
        ):
            if isinstance(record, RawEvent):
                events += 1
            else:
                sessions += 1
    stats = getattr(connector, "last_stats", IngestStats())
    return DryRun(
        connector.name, str(path), version, sessions, events, time.perf_counter() - start, stats
    )


def print_report(report: DryRun) -> None:
    s = report.stats
    print(f"{report.source} connector — dry-run report (read-only, nothing written)")
    print(f"  path:            {report.source_path}")
    print(f"  schema_version:  {report.schema_version}")
    print(f"  files {s.files} · sessions {report.sessions} · events {report.events}")
    print(f"  lines {s.lines} (malformed {s.malformed_lines}) · elapsed {report.elapsed_s:.2f}s")
    print(f"  line types:      {dict(s.lines_by_type)}")
    print(f"  skipped:         {dict(s.skipped)}")
    print(f"  events by kind:  {dict(s.events_by_kind)}")
    print(f"  roles:           {dict(s.roles)}")
    print("  redaction matches by kind:")
    for kind, n in sorted(s.redaction_matches_by_kind.items(), key=lambda x: -x[1]):
        print(f"    {kind:24s} {n}")
    if not s.redaction_matches_by_kind:
        print("    (none)")
    print(f"  events with redaction: {s.events_with_redaction}")
    print(
        f"  oversized (>{SPILL_THRESHOLD_CHARS} chars): {s.oversized_events} events,"
        f" {s.oversized_content_chars} chars would be spilled to blobs"
    )
    for w in s.warnings:
        print(f"  WARNING: {w}")
