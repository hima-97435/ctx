"""Truncation of oversized event text with blob spill (ADR-004).

Input must already be REDACTED (ADR-012): the full content that gets spilled is
what the caller passes in. Content at or below the threshold is kept as-is.
Oversized content is spilled to a blob (fsynced before this function returns, so
the blob exists before the caller inserts the event row) and the kept text is
head + marker + tail.
"""

from dataclasses import dataclass

from ctx.core.blobs import BlobStore
from ctx.core.tokens import estimate_tokens

SPILL_THRESHOLD_CHARS = 64 * 1024
HEAD_CHARS = 4 * 1024
TAIL_CHARS = 4 * 1024


@dataclass(frozen=True)
class PreparedText:
    text: str
    token_estimate: int
    truncated: bool
    blob_ref: str | None


def prepare_text(
    content: str,
    *,
    blob_store: BlobStore,
    spill_threshold: int = SPILL_THRESHOLD_CHARS,
    head: int = HEAD_CHARS,
    tail: int = TAIL_CHARS,
) -> PreparedText:
    """Prepare redacted content for storage: spill if oversized, keep head+tail."""
    if len(content) <= spill_threshold:
        return PreparedText(content, estimate_tokens(content), False, None)

    blob_ref = blob_store.spill(content)  # blob-before-event: exists on disk now
    if head + tail >= len(content):
        kept = content
    else:
        marker = (
            f"\n\n[... truncated {len(content) - head - tail} chars; "
            f"full content: {blob_ref} ...]\n\n"
        )
        kept = content[:head] + marker + content[-tail:]
    return PreparedText(kept, estimate_tokens(kept), True, blob_ref)


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    """Pure head+tail truncation so the result's estimate fits `max_tokens`.

    Used when assembling context (no blob spill: the full text stays in `events`).
    Text that already fits is returned unchanged.
    """
    if estimate_tokens(text) <= max_tokens:
        return text
    marker = "\n[... truncated ...]\n"
    keep = max(max_tokens * 4 - len(marker), 0)
    if keep == 0:
        return text[: max(max_tokens * 4, 0)]
    head = keep - keep // 2
    tail = keep // 2
    return text[:head] + marker + (text[-tail:] if tail else "")
