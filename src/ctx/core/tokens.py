"""Token estimation.

This is the single isolated token-estimate function. Replace the body with a real
tokenizer later; nothing else in the codebase may compute token estimates itself.
"""

_TOKENS_PER_CHAR = 4


def estimate_tokens(text: str) -> int:
    """Estimate token count for a piece of text (v1 heuristic: chars / 4)."""
    return len(text) // _TOKENS_PER_CHAR
