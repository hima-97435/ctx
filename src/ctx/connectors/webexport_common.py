"""Shared plumbing for the official-data-export connectors (ChatGPT, Claude.ai, Gemini).

Source-neutral (ADR-007): archive discovery (a .zip read in place with `zipfile`, or an
extracted directory), member-level denylist enforcement, JSON loading and the
fetch/check_version skeleton. Each connector supplies `select` (which members it
reads), `validate` (structural pin) and `conversations` (mapping to sessions/events).

Nothing is ever extracted to disk: zip members are read into memory. Members that match
a denylist pattern are refused before they are opened (ADR-025).
"""

import fnmatch
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from zipfile import BadZipFile, ZipFile

from ctx.connectors.base import ConnectorError, DenylistViolation, assert_path_allowed
from ctx.connectors.jsonl_common import IngestStats
from ctx.core import paths
from ctx.core.blobs import BlobStore
from ctx.core.model import RawEvent, RawSession
from ctx.core.sync_state import SyncState, fingerprint

MAX_MEMBER_BYTES = 1 << 30  # refuse a single JSON/HTML member larger than 1 GiB


def check_member_allowed(name: str, patterns: tuple[str, ...]) -> None:
    """Raise DenylistViolation if an archive member name matches a denylist glob."""
    base = name.rsplit("/", 1)[-1]
    for pattern in patterns:
        if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(base, pattern):
            raise DenylistViolation(f"archive member {name!r} matches denylist {pattern!r}")


class ExportArchive:
    """A .zip file or an extracted directory; members are posix paths relative to it."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.is_zip = self.path.is_file()
        self._names: list[str] | None = None

    def names(self) -> list[str]:
        if self._names is None:
            self._names = sorted(self._list())
        return self._names

    def _list(self) -> list[str]:
        if self.is_zip:
            try:
                with ZipFile(self.path) as zf:
                    infos = zf.infolist()
            except (BadZipFile, OSError) as exc:
                raise ConnectorError(f"{self.path.name}: not a readable zip file ({exc})") from exc
            names = [i.filename.replace("\\", "/") for i in infos if not i.is_dir()]
        else:
            names = []
            for dirpath, _dirs, files in os.walk(self.path):
                rel = Path(dirpath).relative_to(self.path)
                names += [(rel / f).as_posix() for f in files]
        return [n for n in names if not n.startswith("__MACOSX/") and "/._" not in "/" + n]

    def read(self, name: str, denylist: tuple[str, ...]) -> bytes:
        """Bytes of one member. Denylisted members are refused before being opened."""
        check_member_allowed(name, denylist)
        if self.is_zip:
            try:
                with ZipFile(self.path) as zf:
                    info = next(
                        (i for i in zf.infolist() if i.filename.replace("\\", "/") == name), None
                    )
                    if info is None:
                        raise ConnectorError(f"{self.path.name}: member {name!r} not found")
                    if info.file_size > MAX_MEMBER_BYTES:
                        raise ConnectorError(f"{self.path.name}: member {name!r} is too large")
                    return zf.read(info)
            except (BadZipFile, OSError, RuntimeError) as exc:
                raise ConnectorError(f"{self.path.name}: cannot read {name!r} ({exc})") from exc
        target = (self.path / name).resolve()
        root = self.path.resolve()
        if root not in target.parents or not target.is_file():
            raise ConnectorError(f"{target} is outside the export directory")
        if target.stat().st_size > MAX_MEMBER_BYTES:
            raise ConnectorError(f"{target.name}: member is too large")
        return target.read_bytes()

    def fingerprint(self, members: list[str]) -> list:
        if self.is_zip:
            return fingerprint(self.path)
        return fingerprint(*(self.path / m for m in members))

    def mtime(self) -> float:
        try:
            return self.path.stat().st_mtime
        except OSError:
            return 0.0


def load_json(archive: ExportArchive, member: str, denylist: tuple[str, ...]) -> Any:
    raw = archive.read(member, denylist)
    try:
        return json.loads(raw.decode("utf-8-sig", errors="replace"))
    except json.JSONDecodeError as exc:
        raise ConnectorError(f"{archive.path.name}:{member}: invalid JSON ({exc})") from exc


class WebExportConnector:
    """Base for connectors over official exports. Subclasses set the class attributes."""

    name: str
    dirname: str  # sub-directory of <CTX_HOME>/imports/ that is scanned by plain `ctx sync`
    schema_version_pin: str
    denylist_paths: tuple[str, ...]
    denylist_tables: frozenset[str] = frozenset()

    def __init__(self, source_root: Path | None = None, extra_allowed: Path | None = None) -> None:
        self.source_root = Path(source_root) if source_root else paths.imports_dir() / self.dirname
        self.allowed_paths = [self.source_root]
        if extra_allowed is not None:  # a path the user explicitly passed to `ctx import`
            self.allowed_paths.append(Path(extra_allowed))
        self.last_stats = IngestStats()

    def default_source_path(self) -> Path:
        return self.source_root

    # ---- subclass hooks ---------------------------------------------------------------
    def select(self, names: list[str]) -> list[str]:
        """The archive members this connector reads (empty: not this kind of export)."""
        raise NotImplementedError

    def validate(self, member: str, data: Any) -> None:
        """Structural pin: raise ConnectorError when the parsed member is not our format."""
        raise NotImplementedError

    def conversations(
        self,
        archive: ExportArchive,
        members: list[str],
        blob_store: BlobStore,
        stats: IngestStats,
    ) -> Iterator[tuple[RawSession, list[RawEvent]]]:
        raise NotImplementedError

    def validated_members(self, members: list[str]) -> list[str]:
        """Members whose parsed content is checked by `validate` (default: all JSON)."""
        return [m for m in members if m.lower().endswith(".json")]

    # ---- discovery ------------------------------------------------------------------------
    def _check_access(self, source_path: Path) -> Path:
        last: ConnectorError | None = None
        for root in self.allowed_paths:
            try:
                assert_path_allowed(source_path, root, self.denylist_paths)
                return root
            except DenylistViolation:
                raise
            except ConnectorError as exc:
                last = exc
        assert last is not None
        raise last

    def _archives(self, source_path: Path, stats: IngestStats | None = None) -> list[ExportArchive]:
        self._check_access(source_path)
        path = Path(source_path)
        if not path.exists():
            raise ConnectorError(f"no {self.name} exports found: {path} does not exist")
        found: list[ExportArchive] = []
        if path.is_file():
            if not path.name.lower().endswith(".zip"):
                raise ConnectorError(f"{path.name}: expected a .zip export or a directory")
            archive = ExportArchive(path)
            if not self.select(archive.names()):
                raise ConnectorError(f"{path.name}: not a {self.name} export (no data files)")
            return [archive]
        direct = [f.name for f in path.iterdir() if f.is_file()]
        if self.select(direct):
            return [ExportArchive(path)]
        for child in sorted(path.iterdir()):
            if child.name.startswith("."):
                continue
            is_zip = child.is_file() and child.name.lower().endswith(".zip")
            if not (is_zip or child.is_dir()):
                continue
            self._check_access(child)
            archive = ExportArchive(child)
            if self.select(archive.names()):
                found.append(archive)
            elif stats is not None:
                stats.warn(f"{child.name}: no {self.name} export data inside; ignored")
        found.sort(key=lambda a: (a.mtime(), a.path.name), reverse=True)  # newest first
        return found

    # ---- connector protocol ---------------------------------------------------------------
    def check_version(self, source_path: Path) -> str:
        archives = self._archives(source_path)
        for archive in archives:
            members = self.select(archive.names())
            for member in self.validated_members(members):
                self.validate(member, load_json(archive, member, self.denylist_paths))
        if not archives:
            return f"{self.schema_version_pin}:empty"
        return self.schema_version_pin

    def fetch(
        self,
        source_path: Path,
        *,
        blob_store: BlobStore,
        tmp_dir: Path,
        state: SyncState | None = None,
    ) -> Iterator[RawSession | RawEvent]:
        self.check_version(source_path)  # fail loud before reading content or yielding
        stats = self.last_stats = IngestStats()
        archives = self._archives(source_path, stats)
        stats.files = len(archives)
        state = state or SyncState()
        seen_sessions: set[str] = set()
        seen_events: set[str] = set()
        for archive in archives:  # newest first: its metadata and content win
            members = self.select(archive.names())
            prints = archive.fingerprint(members)  # before reading (ADR-024)
            key = f"archive:{archive.path}"
            if state.is_unchanged(key, prints):
                continue
            convs = list(self.conversations(archive, members, blob_store, stats))
            for session, _events in convs:
                if session.source_session_id not in seen_sessions:
                    seen_sessions.add(session.source_session_id)
                    yield session
            for _session, events in convs:
                for event in events:
                    if event.source_id in seen_events:
                        stats.skipped["duplicate_in_run"] += 1
                        continue
                    seen_events.add(event.source_id)
                    yield event
            state.stage(key, prints)
