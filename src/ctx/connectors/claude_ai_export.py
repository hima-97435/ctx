"""Claude.ai export connector (source name: "claude_ai").

Reads the official data export zip (Settings > Privacy > Export data) or an extracted
copy: `conversations.json` (uuid, name, created_at, chat_messages[...]) and, as
metadata only, `projects.json`. users.json (account details) is never read.

One event per content block (`<conversation uuid>:<message uuid>:<block index>`); when
a message has no `content` blocks its `text` field is used. Attachments with
`extracted_content` and uploaded files become `file` events. If a conversation names a
project (`project_uuid`) found in projects.json, the session's `agent` is
`project:<name>`; ctx projects are never linked from it (ADR-025).
"""

from collections.abc import Iterator
from typing import Any

from ctx.connectors.base import ConnectorError
from ctx.connectors.jsonl_common import IngestStats, build_event, parse_ts, tool_input_text
from ctx.connectors.webexport_common import ExportArchive, WebExportConnector, load_json
from ctx.core.blobs import BlobStore
from ctx.core.model import RawEvent, RawSession
from ctx.core.redact import redact

SOURCE = "claude_ai"
SCHEMA_VERSION_PIN = "claude-ai-export-v1"
DENYLIST_PATHS = (
    "users.json",
    "user.json",
    "*account*.json",
    "*auth*.json",
    ".env",
)
_ROLES = {"human": "user", "assistant": "assistant"}


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _base(name: str) -> str:
    return name.rsplit("/", 1)[-1].lower()


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            _str(c.get("text")) if isinstance(c, dict) else _str(c) for c in content
        ).strip()
    return ""


class ClaudeAiExportConnector(WebExportConnector):
    name = SOURCE
    dirname = "claude_ai"
    schema_version_pin = SCHEMA_VERSION_PIN
    denylist_paths = DENYLIST_PATHS

    def select(self, names: list[str]) -> list[str]:
        convs = [n for n in names if _base(n) == "conversations.json"]
        if not convs:
            return []
        return sorted(convs + [n for n in names if _base(n) == "projects.json"])

    def validated_members(self, members: list[str]) -> list[str]:
        return [m for m in members if _base(m) == "conversations.json"]

    def validate(self, member: str, data: Any) -> None:
        if not isinstance(data, list):
            raise ConnectorError(f"{member}: Claude.ai export must be a JSON list of conversations")
        head = [c for c in data[:20] if isinstance(c, dict)]
        if data and not any(isinstance(c.get("chat_messages"), list) for c in head):
            raise ConnectorError(
                f"{member}: Claude.ai export format unrecognized (no conversation has"
                " 'chat_messages'); failing loud, nothing was written"
            )

    def _project_names(
        self, archive: ExportArchive, members: list[str], stats: IngestStats
    ) -> dict[str, str]:
        names: dict[str, str] = {}
        for member in members:
            if _base(member) != "projects.json":
                continue
            try:
                data = load_json(archive, member, self.denylist_paths)
            except ConnectorError as exc:
                stats.warn(f"projects.json ignored: {exc}")
                continue
            for p in data if isinstance(data, list) else []:
                if isinstance(p, dict) and isinstance(p.get("uuid"), str) and p.get("name"):
                    names[p["uuid"]] = _str(p["name"])
        return names

    def conversations(
        self,
        archive: ExportArchive,
        members: list[str],
        blob_store: BlobStore,
        stats: IngestStats,
    ) -> Iterator[tuple[RawSession, list[RawEvent]]]:
        projects = self._project_names(archive, members, stats)
        for member in members:
            if _base(member) != "conversations.json":
                continue
            for conv in load_json(archive, member, self.denylist_paths):
                if not isinstance(conv, dict) or not isinstance(conv.get("chat_messages"), list):
                    stats.skipped["malformed_conversation"] += 1
                    continue
                cid = conv.get("uuid")
                if not isinstance(cid, str) or not cid:
                    stats.skipped["conversation_without_id"] += 1
                    continue
                yield self._conversation(cid, conv, projects, blob_store, stats)

    def _conversation(
        self,
        cid: str,
        conv: dict,
        projects: dict[str, str],
        blob_store: BlobStore,
        stats: IngestStats,
    ) -> tuple[RawSession, list[RawEvent]]:
        created = parse_ts(conv.get("created_at"))
        events: list[RawEvent] = []
        last_ts = created or 0
        for msg in conv["chat_messages"]:
            if not isinstance(msg, dict) or not isinstance(msg.get("uuid"), str):
                stats.skipped["malformed_message"] += 1
                continue
            sender = _str(msg.get("sender"))
            if sender not in _ROLES:
                stats.skipped[f"unknown_sender:{sender or 'none'}"] += 1
                continue
            ts = parse_ts(msg.get("created_at")) or last_ts
            last_ts = ts
            for idx, kind, text in self._message_items(msg, stats):
                events.append(
                    build_event(
                        source=SOURCE,
                        source_id=f"{cid}:{msg['uuid']}:{idx}",
                        ts=ts,
                        session_ref=cid,
                        role=_ROLES[sender],
                        kind=kind,
                        call_id=None,
                        synthetic=False,
                        content=text,
                        hash_basis=f"{sender}:{kind}:{text}",
                        message_ref=msg["uuid"],
                        blob_store=blob_store,
                        stats=stats,
                    )
                )
        project_ref = conv.get("project_uuid")
        project = projects.get(project_ref) if isinstance(project_ref, str) else None
        name = _str(conv.get("name"))
        session = RawSession(
            source=SOURCE,
            source_session_id=cid,
            started_at=created or (events[0].ts if events else 0),
            ended_at=parse_ts(conv.get("updated_at")) or (events[-1].ts if events else None),
            cwd=None,
            model=_str(conv.get("model")) or None,
            agent=f"project:{redact(project)}" if project else None,
            title=redact(name) if name else None,
        )
        return session, events

    def _message_items(self, msg: dict, stats: IngestStats) -> Iterator[tuple[str, str, str]]:
        """(index, kind, text) per importable piece of one message."""
        produced = False
        blocks = msg.get("content")
        for idx, block in enumerate(blocks if isinstance(blocks, list) else []):
            if not isinstance(block, dict):
                stats.skipped["bad_block"] += 1
                continue
            btype = _str(block.get("type"))
            if btype == "text":
                kind, text = "text", _str(block.get("text"))
            elif btype == "thinking":
                kind, text = "reasoning", _str(block.get("thinking"))
            elif btype == "tool_use":
                kind = f"tool:{_str(block.get('name')) or 'unknown'}"
                text = tool_input_text(block.get("input"))
            elif btype == "tool_result":
                kind, text = "tool_result", _result_text(block.get("content"))
            elif btype in ("image", "document"):
                kind, text = "file", f"[{btype}]"
            else:
                stats.skipped[f"unknown_block:{btype or 'none'}"] += 1
                continue
            if text.strip():
                produced = True
                yield str(idx), kind, text
            else:
                stats.skipped["empty_block"] += 1
        if not produced and _str(msg.get("text")).strip():
            yield "0", "text", _str(msg["text"])  # older exports: no content blocks
        for i, att in enumerate(msg.get("attachments") or []):
            if not isinstance(att, dict):
                continue
            body = _str(att.get("extracted_content"))
            label = f"[attachment: {_str(att.get('file_name')) or 'unnamed'}"
            label += f" ({_str(att.get('file_type'))})]" if att.get("file_type") else "]"
            yield f"att{i}", "file", f"{label}\n{body}" if body.strip() else label
        for i, f in enumerate(msg.get("files") or []):
            if isinstance(f, dict):
                yield f"file{i}", "file", f"[file: {_str(f.get('file_name')) or 'unnamed'}]"
