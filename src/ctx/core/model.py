"""Source-neutral data types produced by connectors and consumed by the sync engine.

These carry already-redacted, already-truncated content (see ADR-012/ADR-017):
project resolution happens later in the sync engine, not in connectors.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class RawSession:
    source: str
    source_session_id: str
    started_at: int  # epoch ms
    ended_at: int | None
    cwd: str | None
    model: str | None
    agent: str | None
    title: str | None


@dataclass(frozen=True)
class RawEvent:
    source: str
    source_id: str
    ts: int  # epoch ms
    session_ref: str | None  # source_session_id; resolved to sessions.id by the sync engine
    role: str | None  # 'user' | 'assistant' | None
    kind: str  # text | reasoning | tool:<name> | patch | file | compaction | prompt
    call_id: str | None
    synthetic: bool
    text: str  # redacted; head+tail when truncated
    token_estimate: int
    truncated: bool
    blob_ref: str | None  # 'blobs/<aa>/<hash>' relative to the data dir
    raw_json: str | None  # reserved; v1 connectors leave it NULL (text/blob carry content)
    content_hash: str  # sha256 of the canonical source content
    message_ref: str | None = None  # source message id, for citations (events.message_id)
