"""Vendor-API model adapters for the refresh script.

Where a vendor exposes a real `/models` endpoint with capability metadata,
we hit it directly instead of relying on the 3-source community merge
(LiteLLM + OpenRouter + models.dev). The vendor's own API is by definition
authoritative for its own catalog — newer than the community-curated
snapshots, with correct flags for newly-released models.

Coverage decision (May 2026, after empirically inspecting each vendor's
``/models`` schema):

- **Rich data → wired here**:
  - Anthropic (`/v1/models`): per-model ``capabilities.{image_input,
    pdf_input, structured_outputs, thinking, citations, code_execution,
    batch, context_management, effort.{low,medium,high,max,xhigh}}`` +
    ``max_input_tokens`` + ``max_tokens``.
  - Mistral (`/v1/models`): ``capabilities.{completion_chat,
    completion_fim, function_calling, fine_tuning, vision,
    classification}`` + ``max_context_length`` + ``aliases`` +
    deprecation info.
  - Google Gemini (`/v1beta/models`): ``inputTokenLimit`` +
    ``outputTokenLimit`` + ``supportedGenerationMethods`` + ``thinking``
    (bool) + temperature defaults. No explicit modalities — modalities
    inferred from description text only when unambiguous.

- **Bare IDs only → not wired**: OpenAI, DeepSeek, Groq, Together, xAI,
  z.ai (their `/v1/models` returns ``id`` + ``owned_by`` and nothing
  about capabilities). The community merge already covers these well
  enough; adapting them for "ID validation only" adds noise without
  improving capability accuracy.

- **Future work**: AWS Bedrock has the richest data of any vendor API
  (full ``inputModalities`` / ``outputModalities`` arrays) but it
  requires ``boto3`` + AWS credentials and is non-trivial to wire from
  a stdlib + httpx-only script. Fireworks / Cohere are rich but less
  common in our user base. Skipped for now.

Each adapter returns sparse capability dicts: only fields the vendor
*explicitly* asserts. Unknown fields are absent, NOT defaulted to False,
so the merge step in the parent script can apply community-merge values
as the fallback without overwriting vendor truth.

Adapters are invoked only when their API key env var is set:
``ANTHROPIC_API_KEY``, ``MISTRAL_API_KEY``, ``GEMINI_API_KEY`` /
``GOOGLE_API_KEY``. Absence is silent — the parent script logs which
vendor sources were attempted vs skipped.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Callable, Iterable

import httpx

# Canonical-key helper is defined in the parent script. Adapters import
# it lazily inside ``iter_vendor_streams`` to avoid an import cycle when
# this module is loaded standalone for testing.

VendorStream = Iterable[tuple[str, str, dict[str, Any]]]


# ---------------------------------------------------------------------------
# Anthropic — /v1/models with rich capabilities.{...}
# ---------------------------------------------------------------------------


_ANTHROPIC_URL = "https://api.anthropic.com/v1/models"


def _fetch_anthropic(api_key: str) -> dict[str, Any]:
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }
    r = httpx.get(_ANTHROPIC_URL, headers=headers, timeout=30.0,
                  params={"limit": 1000})
    r.raise_for_status()
    return r.json()


def _from_anthropic(
    data: dict[str, Any], canonicalize: Callable[[str], str],
) -> VendorStream:
    """Anthropic returns ``data: [ModelInfo]``. Each ``ModelInfo`` has an
    ``id`` and a ``capabilities`` object whose sub-fields are
    ``{supported: bool}`` records.
    """
    for entry in data.get("data", []) or []:
        if not isinstance(entry, dict):
            continue
        raw_id = entry.get("id") or ""
        canon = canonicalize(raw_id)
        if not canon:
            continue
        caps_obj = entry.get("capabilities") or {}

        def _supported(field: str) -> bool | None:
            block = caps_obj.get(field)
            if isinstance(block, dict) and isinstance(block.get("supported"), bool):
                return block["supported"]
            return None

        # Build a SPARSE dict — only include fields Anthropic explicitly
        # answers. Anything absent stays unknown so the community merge
        # can fill in.
        sparse: dict[str, Any] = {}

        max_in = entry.get("max_input_tokens")
        if isinstance(max_in, int):
            sparse["max_input_tokens"] = max_in
        max_out = entry.get("max_tokens")
        if isinstance(max_out, int):
            sparse["max_output_tokens"] = max_out

        image_in = _supported("image_input")
        if image_in is not None:
            sparse["supports_vision"] = image_in
        pdf_in = _supported("pdf_input")
        if pdf_in is not None:
            sparse["supports_pdf_input"] = pdf_in
        thinking_block = caps_obj.get("thinking")
        if isinstance(thinking_block, dict) and isinstance(
            thinking_block.get("supported"), bool,
        ):
            sparse["supports_reasoning"] = thinking_block["supported"]
        structured = _supported("structured_outputs")
        if structured is not None:
            sparse["supports_response_schema"] = structured

        # Anthropic models always support function calling (the Messages
        # API exposes ``tools``). The API doesn't have a specific
        # ``function_calling`` capability key, but all current models
        # support it — leave the field absent here and let the community
        # merge fill in to avoid encoding a model-family assumption.

        # Prompt caching: Anthropic's beta-headers list shows caching is
        # available; per-model availability isn't in the response. Leave
        # absent for now.

        yield canon, f"vendor:anthropic/{raw_id}", sparse


# ---------------------------------------------------------------------------
# Mistral — /v1/models with capabilities.{...} + max_context_length
# ---------------------------------------------------------------------------


_MISTRAL_URL = "https://api.mistral.ai/v1/models"


def _fetch_mistral(api_key: str) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {api_key}"}
    r = httpx.get(_MISTRAL_URL, headers=headers, timeout=30.0)
    r.raise_for_status()
    return r.json()


def _from_mistral(
    data: dict[str, Any], canonicalize: Callable[[str], str],
) -> VendorStream:
    """Mistral returns ``data: [Model]`` where each ``Model`` has an
    ``id``, ``capabilities`` object, ``max_context_length``, ``aliases``
    list, and a ``deprecation`` timestamp."""
    for entry in data.get("data", []) or []:
        if not isinstance(entry, dict):
            continue
        raw_id = entry.get("id") or ""
        canon = canonicalize(raw_id)
        if not canon:
            continue
        if entry.get("deprecation"):
            # Skip explicitly-deprecated models — we don't want them
            # populating the snapshot.
            continue
        caps_obj = entry.get("capabilities") or {}
        sparse: dict[str, Any] = {}

        max_ctx = entry.get("max_context_length")
        if isinstance(max_ctx, int):
            sparse["max_input_tokens"] = max_ctx

        # Booleans are direct fields under capabilities.
        def _bool(field: str) -> bool | None:
            v = caps_obj.get(field)
            return bool(v) if isinstance(v, bool) else None

        vision = _bool("vision")
        if vision is not None:
            sparse["supports_vision"] = vision
        function_calling = _bool("function_calling")
        if function_calling is not None:
            sparse["supports_function_calling"] = function_calling

        yield canon, f"vendor:mistral/{raw_id}", sparse

        # Aliases — emit the same caps under each alias so a user
        # configuring ``mistral-large-latest`` (vs ``mistral-large-2407``)
        # also hits the entry.
        for alias in entry.get("aliases", []) or []:
            alias_canon = canonicalize(alias)
            if alias_canon and alias_canon != canon:
                yield alias_canon, f"vendor:mistral/{raw_id}#alias:{alias}", sparse


# ---------------------------------------------------------------------------
# Google Gemini — /v1beta/models with inputTokenLimit + supportedGenerationMethods
# ---------------------------------------------------------------------------


_GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models"


def _fetch_gemini(api_key: str) -> dict[str, Any]:
    # Gemini paginates: pageSize up to 1000 covers their full catalog
    # comfortably as of 2026.
    r = httpx.get(_GEMINI_URL, params={"key": api_key, "pageSize": 1000},
                  timeout=30.0)
    r.raise_for_status()
    return r.json()


def _from_gemini(
    data: dict[str, Any], canonicalize: Callable[[str], str],
) -> VendorStream:
    """Gemini returns ``models: [Model]``. Each Model has ``name``
    (prefixed with ``models/``), ``inputTokenLimit``,
    ``outputTokenLimit``, ``supportedGenerationMethods`` (list), and a
    ``thinking`` boolean for recent models.

    No explicit modality fields. We treat the description as informative
    but DON'T parse it — too brittle. Modality flags are left absent so
    the community merge fills in.
    """
    for entry in data.get("models", []) or []:
        if not isinstance(entry, dict):
            continue
        raw_name = entry.get("name") or ""
        # Names come in as "models/gemini-2.5-flash"; strip the prefix.
        bare = raw_name.split("/", 1)[1] if raw_name.startswith("models/") else raw_name
        canon = canonicalize(bare)
        if not canon:
            continue

        sparse: dict[str, Any] = {}
        max_in = entry.get("inputTokenLimit")
        if isinstance(max_in, int) and max_in > 0:
            sparse["max_input_tokens"] = max_in
        max_out = entry.get("outputTokenLimit")
        if isinstance(max_out, int) and max_out > 0:
            sparse["max_output_tokens"] = max_out

        thinking = entry.get("thinking")
        if isinstance(thinking, bool):
            sparse["supports_reasoning"] = thinking

        # generateContent in supportedGenerationMethods → chat-capable.
        # Mode default is already "chat" downstream so don't override.
        # Skip embedding-only models — they shouldn't end up in the
        # snapshot's chat-models bucket.
        methods = set(entry.get("supportedGenerationMethods") or [])
        if not (methods & {"generateContent", "streamGenerateContent"}):
            continue

        yield canon, f"vendor:gemini/{bare}", sparse


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


# Adapter registry — name + env-var binding. The actual fetch + parse
# functions are looked up via ``getattr`` on this module at dispatch
# time so that monkeypatching either function (e.g. in tests) updates
# the resolved reference. Storing function refs directly in the tuple
# would freeze the binding at import time and bypass any later patches.
_ADAPTERS_META: list[tuple[str, str]] = [
    ("anthropic", "ANTHROPIC_API_KEY"),
    ("mistral", "MISTRAL_API_KEY"),
    # Gemini accepts either GEMINI_API_KEY or GOOGLE_API_KEY; the helper
    # below tries both.
    ("gemini", "GEMINI_API_KEY"),
]


def _resolve_gemini_key() -> str | None:
    """Gemini docs and SDK both accept ``GEMINI_API_KEY`` *or* the older
    ``GOOGLE_API_KEY``. Try both rather than forcing the user to set a
    specific name."""
    return os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")


def iter_vendor_streams(
    canonicalize: Callable[[str], str],
) -> tuple[list[VendorStream], list[str], list[str]]:
    """Invoke every adapter whose key is set; return per-adapter streams
    plus a parallel ``attempted`` / ``skipped`` log.

    Adapter failures are caught here so one vendor's outage can't break
    the refresh — the script logs the error and falls back to the
    community merge for that vendor's models.
    """
    # Resolve fetch/parse by name on each call so tests can
    # monkeypatch either function and have the new binding picked
    # up here. ``globals()`` is the module's own namespace dict and
    # tracks ``setattr`` updates that monkeypatch performs, so the
    # lookup naturally picks up patched versions.
    namespace = globals()
    streams: list[VendorStream] = []
    attempted: list[str] = []
    skipped: list[str] = []
    for name, env_var in _ADAPTERS_META:
        key = _resolve_gemini_key() if name == "gemini" else os.getenv(env_var)
        if not key:
            skipped.append(f"{name}: no {env_var} in env")
            continue
        fetch_fn = namespace[f"_fetch_{name}"]
        iter_fn = namespace[f"_from_{name}"]
        print(f"  → vendor:{name} (via {env_var})", file=sys.stderr)
        try:
            data = fetch_fn(key)
        except Exception as exc:
            skipped.append(f"{name}: fetch failed — {exc}")
            continue
        attempted.append(name)
        # Materialize to a list so downstream consumers can iterate
        # multiple times if they want and so any iteration errors
        # surface here (where they're catchable) instead of in the
        # merge loop.
        try:
            entries = list(iter_fn(data, canonicalize))
        except Exception as exc:
            skipped.append(f"{name}: parse failed — {exc}")
            attempted.remove(name)
            continue
        streams.append(entries)
        print(f"    ↳ {len(entries)} entries", file=sys.stderr)
    return streams, attempted, skipped
