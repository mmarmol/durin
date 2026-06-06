"""Local LLM provider using llama-cpp-python — no external service needed.

Supports multiple models loaded in parallel, each with its own lock.
Requests are routed by the `model` parameter in chat().
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from durin.extras import ensure_or_note
from durin.providers.base import LLMProvider, LLMResponse

_DEFAULT_CACHE_DIR = Path.home() / ".cache" / "durin" / "models"


@dataclass(frozen=True)
class ModelSpec:
    """Specification for a downloadable GGUF model."""
    name: str
    repo_id: str
    filename: str


MODELS = {
    "qwen3b": ModelSpec(
        name="qwen3b",
        repo_id="Qwen/Qwen2.5-3B-Instruct-GGUF",
        filename="qwen2.5-3b-instruct-q4_k_m.gguf",
    ),
    "gemma2b": ModelSpec(
        name="gemma2b",
        repo_id="bartowski/gemma-2-2b-it-GGUF",
        filename="gemma-2-2b-it-Q4_K_M.gguf",
    ),
}

DEFAULT_MODEL = "qwen3b"


def _ensure_model(spec: ModelSpec, cache_dir: Path | None = None) -> Path:
    """Download GGUF model from HuggingFace if not present."""
    try:
        from huggingface_hub import hf_hub_download
    except ImportError:
        raise ImportError(
            "huggingface-hub is required for local models. "
            "Install with: pip install 'durin[local]'"
        ) from None

    target_dir = cache_dir or _DEFAULT_CACHE_DIR
    target_dir.mkdir(parents=True, exist_ok=True)

    return Path(hf_hub_download(
        repo_id=spec.repo_id,
        filename=spec.filename,
        cache_dir=str(target_dir),
        local_dir=str(target_dir / spec.repo_id.replace("/", "--")),
    ))


_NO_SYSTEM_ROLE_MODELS = frozenset({"gemma2b"})


def _adapt_messages(messages: list[dict[str, Any]], model_name: str) -> list[dict[str, Any]]:
    """Some models don't support system role — merge into first user message."""
    if model_name not in _NO_SYSTEM_ROLE_MODELS:
        return messages

    adapted = []
    system_parts = []
    for msg in messages:
        if msg["role"] == "system":
            system_parts.append(msg["content"])
        else:
            if system_parts and msg["role"] == "user":
                prefix = "\n\n".join(system_parts)
                adapted.append({"role": "user", "content": f"{prefix}\n\n{msg['content']}"})
                system_parts = []
            else:
                adapted.append(msg)
    return adapted or messages


class _ModelInstance:
    """A loaded model with its own inference lock."""

    __slots__ = ("_llama", "_lock", "name")

    def __init__(self, llama, name: str) -> None:
        self._llama = llama
        self._lock = asyncio.Lock()
        self.name = name

    async def generate(
        self,
        messages: list[dict[str, Any]],
        max_tokens: int,
        temperature: float,
    ) -> dict:
        adapted = _adapt_messages(messages, self.name)
        async with self._lock:
            return await asyncio.to_thread(
                self._llama.create_chat_completion,
                messages=adapted,
                max_tokens=max_tokens,
                temperature=temperature,
                top_p=0.9,
            )


class LocalLlamaProvider(LLMProvider):
    """Runs inference locally via llama-cpp-python. Supports multiple models.

    Each model gets its own lock — calls to different models run in parallel.

    Usage:
        provider = LocalLlamaProvider(models=["qwen3b", "gemma2b"])
        # Generator configs reference models by name:
        #   GeneratorConfig(model="qwen3b", ...)
        #   GeneratorConfig(model="gemma2b", ...)
    """

    def __init__(
        self,
        models: list[str] | None = None,
        n_ctx: int = 2048,
        n_gpu_layers: int = -1,
        cache_dir: Path | None = None,
    ) -> None:
        super().__init__(api_key=None, api_base=None)
        self._model_names = models or [DEFAULT_MODEL]
        self._n_ctx = n_ctx
        self._n_gpu_layers = n_gpu_layers
        self._cache_dir = cache_dir
        self._instances: dict[str, _ModelInstance] = {}
        self._load_lock = asyncio.Lock()

    def get_default_model(self) -> str:
        return self._model_names[0]

    async def _get_instance(self, model_name: str) -> _ModelInstance:
        if model_name in self._instances:
            return self._instances[model_name]

        async with self._load_lock:
            if model_name in self._instances:
                return self._instances[model_name]

            spec = MODELS.get(model_name)
            if spec is None:
                if Path(model_name).exists():
                    model_path = model_name
                else:
                    logger.warning("Unknown model '{}', falling back to {}", model_name, DEFAULT_MODEL)
                    spec = MODELS[DEFAULT_MODEL]
                    model_name = DEFAULT_MODEL
                    if model_name in self._instances:
                        return self._instances[model_name]

            if spec is not None:
                model_path = str(_ensure_model(spec, self._cache_dir))

            llama = await asyncio.to_thread(self._load_llama, model_path, model_name)
            instance = _ModelInstance(llama, model_name)
            self._instances[model_name] = instance
            return instance

    def _load_llama(self, model_path: str, name: str):
        try:
            from llama_cpp import Llama
        except ImportError:
            res = ensure_or_note("local_models", config=getattr(self, "_app_config", None))
            try:
                from llama_cpp import Llama
            except ImportError as exc:
                raise ImportError(
                    "llama-cpp-python is required for local models. "
                    + (res.message or "Install with: pip install durin-agent[local]")
                ) from exc

        logger.info("Loading model '{}': {} (n_gpu_layers={})", name, model_path, self._n_gpu_layers)
        return Llama(
            model_path=model_path,
            n_ctx=self._n_ctx,
            n_gpu_layers=self._n_gpu_layers,
            verbose=False,
        )

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 512,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> LLMResponse:
        model_name = model or self._model_names[0]
        instance = await self._get_instance(model_name)

        result = await instance.generate(messages, max_tokens, temperature)

        choice = result["choices"][0]
        content = choice["message"].get("content", "")
        usage = result.get("usage", {})

        return LLMResponse(
            content=content,
            tool_calls=[],
            finish_reason=choice.get("finish_reason", "stop"),
            usage={
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
            },
        )
