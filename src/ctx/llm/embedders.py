"""Cloud embedders (Gemini, NVIDIA NIM) behind the existing `Embedder` interface.

`is_cloud=True`, so the same gate applies as for chat: `embed_events` skips `local_only=1`
and unlinked events, and `retrieve` refuses to embed a query unless scoped to one
`local_only=0` project (`core.embed.check_embedder_allowed` -> `ctx.llm.policy`). Texts are
already redacted by the caller. Specs: `gemini:<model>` and `nvidia:<model>`; keys from the
environment only (see `providers`).
"""

import os
from collections.abc import Callable, Sequence

from ctx.core.embed import Embedder, EmbedderUnavailable, register_embedder
from ctx.llm.http import LLMError, Transport, UrllibTransport, post_json
from ctx.llm.providers import GEMINI_KEYS, GEMINI_URL, NVIDIA_KEYS, NVIDIA_URL, api_key

GEMINI_DEFAULT = "gemini-embedding-001"
GEMINI_DIM = 768  # requested via `dimensions`; the model's native 3072 is needlessly large
NVIDIA_DEFAULT = "nvidia/nv-embedqa-e5-v5"
NVIDIA_DIMS = {"nvidia/nv-embedqa-e5-v5": 1024, "nvidia/nv-embed-v1": 4096}
BATCH = 32


class _OpenAIStyleEmbedder:
    is_cloud = True

    def __init__(
        self,
        kind: str,
        model: str,
        base_url: str,
        key_envs: Sequence[str],
        dim: int,
        extra: dict,
        transport: Transport | None,
        sleep: Callable[[float], None] | None,
    ) -> None:
        self.name = f"{kind}:{model}"
        self.model = model
        self.dim = dim
        self._kind = kind
        self._url = base_url.rstrip("/") + "/embeddings"
        self._keys = tuple(key_envs)
        self._extra = extra
        self._transport = transport or UrllibTransport()
        self._sleep = sleep

    def embed(self, texts: Sequence[str]):
        import numpy as np

        try:
            key = api_key(self._keys, self._kind)
            out: list[list[float]] = []
            for i in range(0, len(texts), BATCH):
                data = post_json(
                    self._transport,
                    self._url,
                    {
                        "model": self.model,
                        "input": list(texts[i : i + BATCH]),
                        "encoding_format": "float",
                        **self._extra,
                    },
                    headers={"Authorization": f"Bearer {key}"},
                    timeout=60.0,
                    label=self._kind,
                    secrets=(key,),
                    **({"sleep": self._sleep} if self._sleep else {}),
                )
                rows = sorted(data.get("data") or [], key=lambda r: r.get("index", 0))
                if len(rows) != len(texts[i : i + BATCH]):
                    raise LLMError(f"{self._kind}: embedding count does not match input")
                out.extend(r["embedding"] for r in rows)
        except (LLMError, KeyError, TypeError) as exc:
            # EmbedderUnavailable is what sync/search callers already degrade on
            raise EmbedderUnavailable(f"{self._kind} embeddings failed: {exc}") from None
        return np.asarray(out, dtype=np.float32)


def gemini_embedder(model: str = "", *, transport: Transport | None = None, sleep=None) -> Embedder:
    return _OpenAIStyleEmbedder(  # type: ignore[return-value]
        "gemini",
        model or GEMINI_DEFAULT,
        os.environ.get("CTX_GEMINI_URL") or GEMINI_URL,
        GEMINI_KEYS,
        GEMINI_DIM,
        {"dimensions": GEMINI_DIM},
        transport,
        sleep,
    )


def nvidia_embedder(model: str = "", *, transport: Transport | None = None, sleep=None) -> Embedder:
    model = model or NVIDIA_DEFAULT
    dim = NVIDIA_DIMS.get(model) or int(os.environ.get("CTX_EMBED_DIM", "0") or 0)
    if not dim:
        raise EmbedderUnavailable(f"unknown dimension for {model!r}; set CTX_EMBED_DIM")
    return _OpenAIStyleEmbedder(  # type: ignore[return-value]
        "nvidia",
        model,
        os.environ.get("CTX_NVIDIA_URL") or NVIDIA_URL,
        NVIDIA_KEYS,
        dim,
        {"input_type": "passage", "truncate": "END"},
        transport,
        sleep,
    )


register_embedder("gemini", gemini_embedder)
register_embedder("nvidia", nvidia_embedder)
