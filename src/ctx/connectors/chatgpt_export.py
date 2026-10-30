"""ChatGPT export connector (source name: "chatgpt").

Reads the official "Export data" zip (Settings > Data controls) or an extracted copy:
`conversations.json` or the sharded `conversations-000.json`, ... Each conversation is a
`mapping` tree of message nodes; the *canonical branch* (current_node back to the root)
is imported as the thread. Other branches (regenerated / edited-away messages) are
skipped and counted as `alternate_branch_message` (ADR-025).

Never read: user.json (account details), message_feedback.json, anything auth/account
shaped. chat.html and media files are ignored. One event per message,
`source_id = <conversation id>:<message id>`. All ChatGPT-specific logic lives here.
"""

from collections.abc import Iterator
from typing import Any

from ctx.connectors.base import ConnectorError
from ctx.connectors.jsonl_common import IngestStats, build_event, parse_ts
from ctx.connectors.webexport_common import ExportArchive, WebExportConnector, load_json
from ctx.core.blobs import BlobStore
from ctx.core.model import RawEvent, RawSession
from ctx.core.redact import redact

SOURCE = "chatgpt"
SCHEMA_VERSION_PIN = "chatgpt-export-v1"
DENYLIST_PATHS = (
    "user.json",
    "message_feedback.json",
    "*account*.json",
    "*auth*.json",
    ".env",
)
_SKIPPED_CONTEXT_TYPES = frozenset({"user_editable_context", "model_editable_context"})


def _is_conversation_file(name: str) -> bool:
    base = name.rsplit("/", 1)[-1].lower()
    if base == "conversations.json":
        return True
    stem = base[len("conversations-") : -len(".json")]
    return base.startswith("conversations-") and base.endswith(".json") and stem.isdigit()


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _parts_text(parts: Any, stats: IngestStats) -> str:
    """Text of a `parts` list: strings as-is, image/audio assets become placeholders."""
    if isinstance(parts, str):
        return parts
    if not isinstance(parts, list):
        return ""
    out: list[str] = []
    for part in parts:
        if isinstance(part, str):
            out.append(part)
        elif isinstance(part, dict):
            ptype = _str(part.get("content_type"))
            if ptype == "image_asset_pointer":
                w, h = part.get("width"), part.get("height")
                out.append(f"[image: {w}x{h}]" if w and h else "[image]")
            elif ptype == "audio_transcription":
                out.append(_str(part.get("text")))
            elif "audio" in ptype:
                out.append("[audio]")
            elif ptype == "real_time_user_audio_video_asset_pointer":
                out.append("[audio/video]")
            else:
                stats.skipped[f"unknown_part:{ptype or 'none'}"] += 1
                out.append(f"[attachment: {ptype or 'unknown'}]")
    return "\n".join(p for p in out if p)


def _thoughts_text(thoughts: Any) -> str:
    if not isinstance(thoughts, list):
        return ""
    chunks = []
    for t in thoughts:
        if isinstance(t, dict):
            chunks.append(
                "\n".join(x for x in (_str(t.get("summary")), _str(t.get("content"))) if x)
            )
    return "\n\n".join(c for c in chunks if c)


def _message_parts(msg: dict, stats: IngestStats) -> tuple[str, str] | None:
    """(kind, text) for a message, or None when it is deliberately not imported."""
    content = msg.get("content")
    content = content if isinstance(content, dict) else {}
    ctype = _str(content.get("content_type"))
    role = _str((msg.get("author") or {}).get("role"))
    recipient = _str(msg.get("recipient"))
    is_call = role == "assistant" and recipient not in ("", "all")
    if role == "tool":
        kind = "tool_result"
    elif is_call:
        kind = f"tool:{recipient}"
    else:
        kind = "text"
    if ctype in ("text", "multimodal_text"):
        return kind, _parts_text(content.get("parts"), stats)
    if ctype == "code":
        lang = _str(content.get("language"))
        text = _str(content.get("text"))
        if kind == "text":
            kind = "tool:code"
        return kind, (text if lang in ("", "unknown") else f"[{lang}]\n{text}")
    if ctype == "execution_output":
        return "tool_result", _str(content.get("text"))
    if ctype == "tether_browsing_display":
        return "tool_result", "\n".join(
            x for x in (_str(content.get("summary")), _str(content.get("result"))) if x
        )
    if ctype == "tether_quote":
        head = " ".join(x for x in (_str(content.get("title")), _str(content.get("url"))) if x)
        return "tool_result", "\n".join(x for x in (head, _str(content.get("text"))) if x)
    if ctype == "system_error":
        return "tool_result", " ".join(
            x for x in (_str(content.get("name")), _str(content.get("text"))) if x
        )
    if ctype == "thoughts":
        return "reasoning", _thoughts_text(content.get("thoughts"))
    if ctype == "reasoning_recap":
        return "reasoning", _str(content.get("content"))
    if ctype in _SKIPPED_CONTEXT_TYPES:  # profile / memory dumps: not conversation content
        stats.skipped[f"personal_context:{ctype}"] += 1
        return None
    stats.skipped[f"unknown_content_type:{ctype or 'none'}"] += 1
    return None


def _canonical_chain(mapping: dict, current: Any, stats: IngestStats) -> list[str]:
    """Node ids root -> current_node. Falls back to the newest leaf without current_node."""
    if not isinstance(current, str) or current not in mapping:
        leaves = [
            nid
            for nid, node in mapping.items()
            if isinstance(node, dict)
            and not [c for c in (node.get("children") or []) if c in mapping]
        ]
        if not leaves:
            return []
        stats.skipped["no_current_node"] += 1

        def newest(nid: str) -> tuple[float, str]:
            msg = mapping[nid].get("message") or {}
            t = msg.get("create_time") if isinstance(msg, dict) else None
            return (float(t) if isinstance(t, int | float) else 0.0, nid)

        current = max(leaves, key=newest)
    chain: list[str] = []
    seen: set[str] = set()
    node: Any = current
    while isinstance(node, str) and node in mapping and node not in seen:
        seen.add(node)
        chain.append(node)
        entry = mapping[node]
        node = entry.get("parent") if isinstance(entry, dict) else None
    chain.reverse()
    return chain


class ChatGPTExportConnector(WebExportConnector):
    name = SOURCE
    dirname = "chatgpt"
    schema_version_pin = SCHEMA_VERSION_PIN
    denylist_paths = DENYLIST_PATHS

    def select(self, names: list[str]) -> list[str]:
        return sorted(n for n in names if _is_conversation_file(n))

    def validate(self, member: str, data: Any) -> None:
        if not isinstance(data, list):
            raise ConnectorError(f"{member}: ChatGPT export must be a JSON list of conversations")
        head = [c for c in data[:20] if isinstance(c, dict)]
        if data and not any(isinstance(c.get("mapping"), dict) for c in head):
            raise ConnectorError(
                f"{member}: ChatGPT export format unrecognized (no conversation has a 'mapping');"
                " failing loud, nothing was written"
            )

    def conversations(
        self,
        archive: ExportArchive,
        members: list[str],
        blob_store: BlobStore,
        stats: IngestStats,
    ) -> Iterator[tuple[RawSession, list[RawEvent]]]:
        for member in members:
            for conv in load_json(archive, member, self.denylist_paths):
                if not isinstance(conv, dict) or not isinstance(conv.get("mapping"), dict):
                    stats.skipped["malformed_conversation"] += 1
                    continue
                cid = conv.get("conversation_id") or conv.get("id")
                if not isinstance(cid, str) or not cid:
                    stats.skipped["conversation_without_id"] += 1
                    continue
                yield self._conversation(cid, conv, blob_store, stats)

    def _conversation(
        self, cid: str, conv: dict, blob_store: BlobStore, stats: IngestStats
    ) -> tuple[RawSession, list[RawEvent]]:
        mapping = conv["mapping"]
        chain = _canonical_chain(mapping, conv.get("current_node"), stats)
        on_chain = set(chain)
        for nid, node in mapping.items():
            if (
                nid not in on_chain
                and isinstance(node, dict)
                and isinstance(node.get("message"), dict)
            ):
                stats.skipped["alternate_branch_message"] += 1
        created = parse_ts(conv.get("create_time"))
        updated = parse_ts(conv.get("update_time"))
        last_ts = created or 0
        model: str | None = None
        events: list[RawEvent] = []
        for nid in chain:
            node = mapping[nid]
            msg = node.get("message") if isinstance(node, dict) else None
            if not isinstance(msg, dict):
                continue  # the synthetic root node
            ts = parse_ts(msg.get("create_time")) or last_ts
            last_ts = ts
            meta = msg.get("metadata") if isinstance(msg.get("metadata"), dict) else {}
            if meta.get("is_visually_hidden_from_conversation"):
                stats.skipped["hidden_message"] += 1
                continue
            parts = _message_parts(msg, stats)
            if parts is None:
                continue
            kind, text = parts
            if not text.strip():
                stats.skipped["empty_message"] += 1
                continue
            arole = _str((msg.get("author") or {}).get("role"))
            if arole in ("user", "assistant"):
                role: str | None = arole
            else:
                role = None  # system / tool
            if arole == "assistant" and isinstance(meta.get("model_slug"), str):
                model = meta["model_slug"]
            mid = _str(msg.get("id")) or nid
            events.append(
                build_event(
                    source=SOURCE,
                    source_id=f"{cid}:{mid}",
                    ts=ts,
                    session_ref=cid,
                    role=role,
                    kind=kind,
                    call_id=None,
                    synthetic=arole == "system",
                    content=text,
                    hash_basis=f"{arole}:{kind}:{text}",
                    message_ref=mid,
                    blob_store=blob_store,
                    stats=stats,
                )
            )
        started = created or (events[0].ts if events else 0)
        title = _str(conv.get("title"))
        session = RawSession(
            source=SOURCE,
            source_session_id=cid,
            started_at=started,
            ended_at=updated or (events[-1].ts if events else None),
            cwd=None,
            model=model or (_str(conv.get("default_model_slug")) or None),
            agent=None,
            title=redact(title) if title else None,
        )
        return session, events
