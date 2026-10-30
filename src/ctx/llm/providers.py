"""Chat providers: Ollama, and OpenAI-compatible endpoints (NVIDIA NIM, Gemini, generic).

Stdlib HTTP only (`ctx.llm.http`). API keys come ONLY from environment variables, are read
at call time, are never stored, logged or included in an error message. Every `chat()`
requires a policy `Clearance` (see `ctx.llm.policy`), so no call path skips the gate.
"""

import os
from collections.abc import Callable, Sequence
from urllib.parse import urlparse

from ctx.llm.http import LLMError, Transport, UrllibTransport, post_json
from ctx.llm.policy import require_clearance

Message = dict[str, str]  # {"role": "system"|"user"|"assistant", "content": str}

OLLAMA_URL = "http://localhost:11434"
NVIDIA_URL = "https://integrate.api.nvidia.com/v1"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
GENERIC_URL = "https://api.openai.com/v1"

NVIDIA_KEYS = ("CTX_NVIDIA_API_KEY", "NVIDIA_API_KEY")
GEMINI_KEYS = ("GEMINI_API_KEY",)
GENERIC_KEYS = ("CTX_OPENAI_API_KEY", "OPENAI_API_KEY")

DEFAULT_MODELS = {
    "ollama": "llama3.2",
    "nvidia": "meta/llama-3.1-70b-instruct",
    "gemini": "gemini-2.5-flash",
    "openai": "gpt-4o-mini",
}


class MissingKey(LLMError):
    """No API key in the environment. The message names variables, never values."""


def api_key(env_names: Sequence[str], label: str) -> str:
    for name in env_names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    raise MissingKey(f"{label}: no API key; set {' or '.join(env_names)} in your environment")


def is_loopback(base_url: str) -> bool:
    return (urlparse(base_url).hostname or "") in {"localhost", "127.0.0.1", "::1"}


class Provider:
    name: str
    model: str
    is_cloud: bool

    def chat(
        self,
        messages: Sequence[Message],
        *,
        clearance: object,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> str:
        raise NotImplementedError


class LocalProvider(Provider):
    """Ollama over HTTP (`/api/chat`). Data never leaves the machine."""

    is_cloud = False

    def __init__(
        self,
        model: str | None = None,
        base_url: str | None = None,
        *,
        transport: Transport | None = None,
        sleep: Callable[[float], None] | None = None,
        timeout: float = 300.0,
    ) -> None:
        self.name = "ollama"
        self.model = model or os.environ.get("CTX_LLM_MODEL") or DEFAULT_MODELS["ollama"]
        self.base_url = (base_url or os.environ.get("CTX_OLLAMA_URL") or OLLAMA_URL).rstrip("/")
        self._transport = transport or UrllibTransport()
        self._sleep = sleep
        self._timeout = timeout

    def chat(self, messages, *, clearance, max_tokens=1024, temperature=0.0) -> str:
        require_clearance(clearance, self.name)
        data = post_json(
            self._transport,
            f"{self.base_url}/api/chat",
            {
                "model": self.model,
                "messages": list(messages),
                "stream": False,
                "options": {"num_predict": max_tokens, "temperature": temperature},
            },
            timeout=self._timeout,
            label="ollama",
            **({"sleep": self._sleep} if self._sleep else {}),
        )
        content = (data.get("message") or {}).get("content")
        if not isinstance(content, str):
            raise LLMError("ollama: response has no message content")
        return content


class OpenAICompatibleProvider(Provider):
    """`POST {base_url}/chat/completions` with a Bearer key from the environment.

    `is_cloud` is true unless `base_url` is loopback (a local vLLM / LM Studio server)."""

    def __init__(
        self,
        name: str,
        base_url: str,
        model: str,
        key_envs: Sequence[str],
        *,
        transport: Transport | None = None,
        sleep: Callable[[float], None] | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.key_envs = tuple(key_envs)
        self.is_cloud = not is_loopback(self.base_url)
        self._transport = transport or UrllibTransport()
        self._sleep = sleep
        self._timeout = timeout

    def chat(self, messages, *, clearance, max_tokens=1024, temperature=0.0) -> str:
        require_clearance(clearance, self.name)
        key = api_key(self.key_envs, self.name)
        data = post_json(
            self._transport,
            f"{self.base_url}/chat/completions",
            {
                "model": self.model,
                "messages": list(messages),
                "max_tokens": max_tokens,
                "temperature": temperature,
            },
            headers={"Authorization": f"Bearer {key}"},
            timeout=self._timeout,
            label=self.name,
            secrets=(key,),
            **({"sleep": self._sleep} if self._sleep else {}),
        )
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise LLMError(f"{self.name}: response has no choices[0].message.content") from None
        if not isinstance(content, str):
            raise LLMError(f"{self.name}: response content is not text")
        return content


class GeminiProvider(OpenAICompatibleProvider):
    """Gemini through Google's OpenAI-compatible endpoint (no SDK)."""

    def __init__(self, model: str | None = None, **kw) -> None:
        super().__init__(
            "gemini",
            os.environ.get("CTX_GEMINI_URL") or GEMINI_URL,
            model or os.environ.get("CTX_LLM_MODEL") or DEFAULT_MODELS["gemini"],
            GEMINI_KEYS,
            **kw,
        )


PROVIDER_NAMES = ("ollama", "nvidia", "gemini", "openai")


def default_provider_name() -> str:
    return os.environ.get("CTX_LLM_PROVIDER") or "ollama"


def get_provider(name: str | None = None, model: str | None = None, **kw) -> Provider:
    """Build a provider by name. `openai` is the generic preset (`CTX_OPENAI_BASE_URL`)."""
    name = (name or default_provider_name()).lower()
    env_model = os.environ.get("CTX_LLM_MODEL")
    if name in ("ollama", "local"):
        return LocalProvider(model, **kw)
    if name == "nvidia":
        return OpenAICompatibleProvider(
            "nvidia",
            os.environ.get("CTX_NVIDIA_URL") or NVIDIA_URL,
            model or env_model or DEFAULT_MODELS["nvidia"],
            NVIDIA_KEYS,
            **kw,
        )
    if name == "gemini":
        return GeminiProvider(model, **kw)
    if name in ("openai", "generic"):
        return OpenAICompatibleProvider(
            "openai",
            os.environ.get("CTX_OPENAI_BASE_URL") or GENERIC_URL,
            model or env_model or DEFAULT_MODELS["openai"],
            GENERIC_KEYS,
            **kw,
        )
    raise ValueError(f"unknown provider {name!r}; use one of: {', '.join(PROVIDER_NAMES)}")
