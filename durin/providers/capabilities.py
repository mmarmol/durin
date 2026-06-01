"""Model capability metadata + lookup.

Source of truth (in priority order):

1. **Explicit override** — the user can declare capabilities for a model
   in config under ``model_capabilities``. Always wins.
2. **Vendored consensus snapshot** — ``model_capabilities.json`` next
   to this module, built from three independent public sources
   (LiteLLM, OpenRouter, models.dev) merged by ``scripts/
   refresh_model_capabilities.py``. Capabilities are OR-merged across
   sources, with each record tracking which sources confirmed it.
3. **Heuristic by model name prefix** — last-resort for custom/local
   models the snapshot doesn't know about. Keeps zero-config working
   for "the obvious cases" (claude → vision, glm → no vision, etc.).
4. **Pessimistic default** — if all three miss, we assume **no** vision,
   audio, video, or pdf; some function-calling support; reasonable
   token bounds. Better to under-promise than to crash later.

The returned :class:`ModelCapabilities` always carries a ``source``
field so callers can tell whether they're working from authoritative
data or a guess. Tools that delegate to auxiliary models (vision,
audio, pdf bridges) only enable themselves when the primary model has
``source != "heuristic"`` AND the relevant capability is False — i.e.
we're confident the primary genuinely lacks the modality.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

_CAPS_JSON_PATH = Path(__file__).parent / "data" / "model_capabilities.json"


@dataclass(frozen=True, slots=True)
class ModelCapabilities:
    """Resolved capability snapshot for one model.

    All flags default to **False / None** when unknown — callers should
    treat ``False`` and "unknown" the same for unsafe capabilities
    (vision, audio, pdf) and explicitly check ``source`` if they need
    to distinguish.

    Naming note — input modalities map to OpenRouter's taxonomy
    (Text / Image / File / Audio / Video) as follows:

    - Text   → always assumed
    - Image  → ``supports_vision``
    - File   → ``supports_pdf_input`` (LiteLLM's specific term; the
      practical scope is the same)
    - Audio  → ``supports_audio_input``
    - Video  → ``supports_video_input``

    We keep the LiteLLM names because the vendored snapshot is the
    primary data source; a future OpenRouter-API source can populate
    the same fields without renaming.
    """

    model: str
    provider: str | None = None
    # Context window
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    mode: str = "chat"
    # Input modalities (what the model accepts beyond text)
    supports_vision: bool = False
    supports_audio_input: bool = False
    supports_pdf_input: bool = False
    supports_video_input: bool = False
    # Output modalities (what the model can emit beyond text)
    supports_audio_output: bool = False
    supports_image_output: bool = False
    # Behavioral capabilities
    supports_function_calling: bool = False
    supports_streaming: bool = True
    supports_reasoning: bool = False
    supports_prompt_caching: bool = False
    supports_response_schema: bool = False
    # Provenance
    source: str = "default"  # "override" | "litellm" | "heuristic" | "default"


# ---------------------------------------------------------------------------
# Snapshot loading
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _load_capabilities_snapshot() -> dict[str, dict[str, Any]]:
    """Read the vendored consensus snapshot once and cache it.

    Expected schema (produced by ``scripts/refresh_model_capabilities.py``)::

        {
          "schema_version": 1,
          "generated_at": "...",
          "sources": {...},
          "models": {
            "<canonical_key>": {
              "max_input_tokens": int|null,
              "max_output_tokens": int|null,
              "mode": "chat",
              "supports_*": bool,
              "_sources": ["litellm:...", "openrouter:...", ...]
            }
          }
        }

    Canonical keys are lowercased bare model names (provider/gateway
    prefix already stripped by the refresh script). Returns the
    ``models`` dict directly — callers don't need the metadata
    envelope. Returns ``{}`` if the file is missing or malformed.
    """
    if not _CAPS_JSON_PATH.exists():
        return {}
    try:
        text = _CAPS_JSON_PATH.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    models = data.get("models")
    if isinstance(models, dict):
        return models
    # Backward compatibility: tolerate the older flat-dict shape.
    data.pop("sample_spec", None)
    return data


_KNOWN_PROVIDER_DOTS = frozenset({
    "anthropic", "amazon", "meta", "mistral", "cohere", "ai21",
    "stability", "openai", "qwen", "deepseek", "moonshot", "zai",
})


def _canonical_lookup_key(model: str) -> str:
    """Reduce a user-supplied model id to the consensus-file canonical
    form: lowercased, no provider/gateway prefix, no bedrock dot prefix.

    Mirrors the normalisation performed by
    ``scripts/refresh_model_capabilities.py:_canonical_key`` so any
    incoming variant lands on the same bucket the snapshot uses.
    """
    if not model:
        return ""
    key = model.strip()
    if "/" in key:
        key = key.rsplit("/", 1)[1]
    if "." in key:
        head, _, rest = key.partition(".")
        if head.lower() in _KNOWN_PROVIDER_DOTS and rest:
            key = rest
    return key.lower()


def _candidate_keys(model: str, provider: str | None) -> list[str]:
    """Ordered list of keys to try in the snapshot.

    The consensus file is keyed by lowercased bare names, so the
    primary lookup uses :func:`_canonical_lookup_key`. We also keep
    the raw / provider-qualified forms as fallbacks for compatibility
    with external callers that pre-process model ids elsewhere.
    """
    model = (model or "").strip()
    provider = (provider or "").strip() or None
    if not model:
        return []
    keys: list[str] = [_canonical_lookup_key(model)]
    # Compatibility variants — only ever match the older flat-dict
    # snapshot shape; the consensus file ignores them.
    if provider:
        keys.append(f"{provider}/{model}")
        if "/" not in model:
            keys.append(f"{provider}.{model}")
    keys.append(model)
    if "/" in model:
        keys.append(model.split("/", 1)[1])
    # De-dup while preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for k in keys:
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _from_consensus_entry(
    model: str, provider: str | None, entry: Mapping[str, Any],
) -> ModelCapabilities:
    """Convert one consensus-file entry into our dataclass.

    The consensus file's entries are already normalised (booleans per
    capability, ``max_input_tokens`` / ``max_output_tokens`` numbers),
    so this is a straight field copy. ``source="litellm"`` retained
    for backwards-compat with callers that branched on the old label;
    the consensus file is in fact a merge of three sources.
    """
    return ModelCapabilities(
        model=model,
        provider=provider,
        max_input_tokens=entry.get("max_input_tokens"),
        max_output_tokens=entry.get("max_output_tokens"),
        mode=entry.get("mode") or "chat",
        supports_vision=bool(entry.get("supports_vision")),
        supports_audio_input=bool(entry.get("supports_audio_input")),
        supports_pdf_input=bool(entry.get("supports_pdf_input")),
        supports_video_input=bool(entry.get("supports_video_input")),
        supports_audio_output=bool(entry.get("supports_audio_output")),
        supports_image_output=bool(entry.get("supports_image_output")),
        supports_function_calling=bool(entry.get("supports_function_calling")),
        supports_streaming=True,  # safe default — explicit overrides via override map
        supports_reasoning=bool(entry.get("supports_reasoning")),
        supports_prompt_caching=bool(entry.get("supports_prompt_caching")),
        supports_response_schema=bool(entry.get("supports_response_schema")),
        source="snapshot",
    )


# ---------------------------------------------------------------------------
# Heuristic fallback
# ---------------------------------------------------------------------------

# Each entry maps a lowercase prefix to a dict of capability flags. Order
# matters — more specific prefixes must come before broader ones. The
# heuristic is intentionally narrow: it only covers families where the
# default-on capabilities are well-known. Unknown models fall through to
# the pessimistic default.
_HEURISTIC_RULES: tuple[tuple[str, dict[str, Any]], ...] = (
    # Anthropic
    ("claude-3", {"supports_vision": True, "supports_pdf_input": True,
                  "supports_function_calling": True, "supports_prompt_caching": True}),
    ("claude-haiku", {"supports_vision": True, "supports_pdf_input": True,
                      "supports_function_calling": True, "supports_prompt_caching": True}),
    ("claude-sonnet", {"supports_vision": True, "supports_pdf_input": True,
                       "supports_function_calling": True, "supports_prompt_caching": True}),
    ("claude-opus", {"supports_vision": True, "supports_pdf_input": True,
                     "supports_function_calling": True, "supports_prompt_caching": True}),
    ("claude", {"supports_vision": True, "supports_function_calling": True}),
    # OpenAI
    ("gpt-4o", {"supports_vision": True, "supports_audio_input": True,
                "supports_pdf_input": True, "supports_function_calling": True}),
    ("gpt-4", {"supports_vision": True, "supports_function_calling": True}),
    ("gpt-5", {"supports_vision": True, "supports_function_calling": True,
               "supports_reasoning": True}),
    ("o1", {"supports_function_calling": True, "supports_reasoning": True}),
    ("o3", {"supports_function_calling": True, "supports_reasoning": True}),
    # Google Gemini — broad multimodal
    ("gemini", {"supports_vision": True, "supports_audio_input": True,
                "supports_pdf_input": True, "supports_video_input": True,
                "supports_function_calling": True}),
    # Chinese-region & open-weight families — typically text-only,
    # newer "vl" variants are vision-capable; the JSON snapshot covers
    # the variants we'd actually configure.
    ("glm", {"supports_function_calling": True}),
    ("qwen", {"supports_function_calling": True}),
    ("deepseek", {"supports_function_calling": True}),
    ("kimi", {"supports_function_calling": True}),
    ("moonshot", {"supports_function_calling": True}),
    ("mistral", {"supports_function_calling": True}),
    ("llama-3", {"supports_function_calling": True}),
    ("llama-2", {}),
)


def _heuristic_capabilities(
    model: str, provider: str | None,
) -> ModelCapabilities:
    """Pattern-match the model name for last-resort capability inference."""
    needle = (model or "").lower()
    # Strip provider prefix so 'anthropic/claude-3-...' matches 'claude-3'.
    if "/" in needle:
        needle = needle.split("/", 1)[1]
    if "." in needle:
        # Bedrock-style 'anthropic.claude-3-...' — strip the leading
        # provider segment when it isn't part of the model family name.
        prefix, _, rest = needle.partition(".")
        if prefix in {"anthropic", "amazon", "meta", "mistral", "cohere",
                      "ai21", "stability"}:
            needle = rest
    for prefix, flags in _HEURISTIC_RULES:
        if needle.startswith(prefix):
            return ModelCapabilities(
                model=model,
                provider=provider,
                source="heuristic",
                **flags,
            )
    return ModelCapabilities(model=model, provider=provider, source="default")


# ---------------------------------------------------------------------------
# Public lookup
# ---------------------------------------------------------------------------


def _apply_override(
    base: ModelCapabilities, override: Mapping[str, Any],
) -> ModelCapabilities:
    """Return a new dataclass with override fields layered on top."""
    fields = {
        "model": base.model,
        "provider": base.provider,
        "max_input_tokens": base.max_input_tokens,
        "max_output_tokens": base.max_output_tokens,
        "mode": base.mode,
        "supports_vision": base.supports_vision,
        "supports_audio_input": base.supports_audio_input,
        "supports_pdf_input": base.supports_pdf_input,
        "supports_video_input": base.supports_video_input,
        "supports_audio_output": base.supports_audio_output,
        "supports_image_output": base.supports_image_output,
        "supports_function_calling": base.supports_function_calling,
        "supports_streaming": base.supports_streaming,
        "supports_reasoning": base.supports_reasoning,
        "supports_prompt_caching": base.supports_prompt_caching,
        "supports_response_schema": base.supports_response_schema,
        "source": "override",
    }
    for k, v in override.items():
        if k in fields and v is not None:
            fields[k] = v
    return ModelCapabilities(**fields)


def get_model_capabilities(
    model: str,
    provider: str | None = None,
    overrides: Mapping[str, Mapping[str, Any]] | None = None,
) -> ModelCapabilities:
    """Resolve a model's capability snapshot.

    Resolution order: override → vendored snapshot → heuristic → pessimistic
    default. The returned dataclass always has a ``source`` field naming
    the layer that produced it; callers that want to gate behavior on
    "we genuinely know the model lacks vision" should require
    ``source in {'litellm', 'override'} and not supports_vision``.

    *overrides* maps either the bare model name or a ``provider/model``
    key to a partial dict of capability fields. Provider/model keys win
    over bare names. Useful for declaring a custom local model's caps
    without needing to ship a JSON entry.
    """
    if not model:
        return ModelCapabilities(model="", provider=provider, source="default")

    snapshot = _load_capabilities_snapshot()
    candidates = _candidate_keys(model, provider)

    base: ModelCapabilities | None = None
    for key in candidates:
        entry = snapshot.get(key)
        if entry:
            base = _from_consensus_entry(model, provider, entry)
            break
    if base is None:
        base = _heuristic_capabilities(model, provider)

    # Override layer applies on top of whatever we resolved.
    if overrides:
        # Match the most specific key first.
        for key in candidates:
            ov = overrides.get(key)
            if ov:
                return _apply_override(base, ov)
    return base


def known_models_count() -> int:
    """Number of model entries available in the vendored snapshot.

    Useful for diagnostics (e.g. a ``durin doctor`` command later) and
    for tests that want to assert the snapshot actually loaded.
    """
    return len(_load_capabilities_snapshot())
