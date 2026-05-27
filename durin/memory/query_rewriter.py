"""LLM-based query rewriter for memory_search (G3.b).

Background: durin's vector retrieval uses ``paraphrase-multilingual-
MiniLM-L12-v2``. When the user query and the relevant memory body use
different vocabulary (e.g. query "What state did X visit?" vs memory
"X took a sunset pic near Fort Wayne"), the cosine similarity is weak
and the right memory drops out of top-K.

This module produces multiple rewrites of a query before the search,
using an LLM as semantic expander. Each rewrite is searched separately
and results are merged via Reciprocal Rank Fusion (RRF). The LLM also
extracts entity refs and predicate hints to feed the entity_ranker
without depending on AliasIndex's regex-strict alias lookup.

Multilingual by design — the LLM (glm-5.1 / gpt-4o-mini / Claude) is
the only component that touches the query text, so any language the
LLM handles is supported. Specifically tested against EN/ES/ZH/JA/KO
plus code-switching.

Failure mode is graceful: if the LLM is unavailable or returns garbage,
the rewriter returns a passthrough :class:`QueryRewrite` containing
only the original query, so callers see no regression vs. the
no-rewrite baseline.

Per glm-5.1 review (2026-05-25):
- Cache uses ``asyncio.Lock`` because the caller (``memory_search``)
  is async; a global dict without locking would race.
- Cache key is NFC-normalized + whitespace-collapsed + fullwidth→
  halfwidth to avoid trivial misses (especially with CJK IME input).
- LLM output is parsed with ``json_repair`` to tolerate trailing
  commas, comments, and other small-model JSON quirks.
- Code fences are stripped with a regex that handles ```````,
  `````json``, and ``~~~`` variants.
- The prompt includes two few-shot examples (EN + ZH) — small models
  need concrete demonstration of "orthogonal vocabulary" expansion.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Union

logger = logging.getLogger(__name__)

__all__ = [
    "QueryRewrite",
    "rewrite_query",
    "rewrite_query_async",
    "clear_cache",
    "resolve_memory_preset",
    "build_memory_llm_invoke",
]


# Callable shapes accepted by ``rewrite_query_async``. The async form
# is the natural fit for provider.chat_with_retry; the sync form is
# preserved for tests + the legacy default_llm_invoke path.
SyncLLMInvoke = Callable[..., str]
AsyncLLMInvoke = Callable[..., Awaitable[str]]
AnyLLMInvoke = Union[SyncLLMInvoke, AsyncLLMInvoke]


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QueryRewrite:
    """Output of the rewriter.

    Fields:
        intent: One of ``factual_lookup | list | temporal | comparison
            | open_ended``. Hint for downstream consumers.
        entities: Entity refs (``<type>:<slug>``) the LLM extracted from
            the query. May be empty.
        predicates: Predicate names (``email | address | role | ...``)
            when the query is asking about a specific attribute. May be
            empty.
        rewrites: All alternate phrasings, **always including the
            original as the first element**. Capped at 5 total.
        language_hint: ISO 639-1 code (``en | es | zh | ja | ko | ...``)
            or empty if the LLM didn't detect one.
        used_llm: ``True`` when the LLM produced this rewrite,
            ``False`` for passthrough (LLM failed or unavailable).
    """

    intent: str
    entities: tuple[str, ...]
    predicates: tuple[str, ...]
    rewrites: tuple[str, ...]
    language_hint: str
    used_llm: bool


# ---------------------------------------------------------------------------
# Cache (process-shared; thread-locked, not asyncio-locked)
# ---------------------------------------------------------------------------
#
# Lock choice — important fix vs the original asyncio.Lock design:
#
# An asyncio.Lock is bound to the event loop it was first created in.
# Durin's bench harness creates a fresh event loop per QA via
# ``asyncio.run``, so a module-level asyncio lock from QA #1 blocks
# QA #2 forever (cross-loop usage). Smoke run 2026-05-26 hit this
# as session-save timeout + empty-answer fails.
#
# Within a single agent run, memory_search.execute is awaited
# sequentially (one in-flight call at a time), so no asyncio-level
# concurrency is actually involved. The lock is only needed because
# durin's runner may dispatch tools on a worker thread (e.g. via
# asyncio.to_thread) and tests may exercise the cache from many
# threads at once. A ``threading.Lock`` covers both cases without
# the event-loop binding.


import threading


_CACHE: dict[str, tuple[float, QueryRewrite]] = {}
_CACHE_LOCK = threading.Lock()
_TTL_SECONDS = 600  # 10 min — compromise between hit rate and freshness
_MAX_CACHE_ENTRIES = 512  # bounded; LRU-ish eviction when crossed

# LLM call timeout for the rewrite step. Tight bound — when z.ai is
# throttled the rewrite must give up fast and fall back to passthrough,
# otherwise it eats the agent loop's per-iteration budget and the
# harness times the whole QA out (verified failure mode on 102-bench
# run 2026-05-26 06:14: 4 of first 4 QAs failed with empty answer +
# FileNotFoundError on session checkpoint after the agent loop hit
# 90s).
_LLM_TIMEOUT_SECONDS = 60.0


# ---------------------------------------------------------------------------
# Cache key normalization (CJK-aware)
# ---------------------------------------------------------------------------


_FULLWIDTH_TO_HALFWIDTH_OFFSET = 0xFEE0  # FF21 ('Ａ') - 0041 ('A')


def _fullwidth_to_halfwidth(text: str) -> str:
    """Convert full-width Latin/digit chars to half-width.

    CJK input methods often produce full-width characters
    (``Ａ`` U+FF21, ``１`` U+FF11) when the user is typing Latin text
    with an IME engaged. These are visually identical to half-width
    but byte-distinct, causing cache misses. Half-width katakana
    (U+FF65–FF9F) is **not** converted — those are legitimate Japanese
    usage.
    """
    out: list[str] = []
    for ch in text:
        cp = ord(ch)
        if 0xFF01 <= cp <= 0xFF5E:  # Full-width ! to ~
            out.append(chr(cp - _FULLWIDTH_TO_HALFWIDTH_OFFSET))
        elif cp == 0x3000:  # Full-width (ideographic) space
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


def _normalize_cache_key(query: str) -> str:
    """NFC + whitespace collapse + fullwidth conversion for stable keying."""
    key = unicodedata.normalize("NFC", query)
    key = _fullwidth_to_halfwidth(key)
    key = re.sub(r"\s+", " ", key).strip()
    # Lowercasing is safe across scripts: CJK has no case, Latin
    # benefits from case-insensitive matching.
    return key.lower()


# ---------------------------------------------------------------------------
# Prompt template
# ---------------------------------------------------------------------------


_PROMPT_TEMPLATE = """\
You are a query rewriter for a multilingual memory retrieval system.

Given the USER QUERY below, produce STRICT JSON with five fields:

1. "intent": one of [factual_lookup, list, temporal, comparison, open_ended]
2. "entities": list of entity refs in the format "<type>:<slug>". Use
   lowercase slugs. Types are open vocabulary; common ones include
   person, project, topic, place, event. Omit if nothing found.
3. "predicates": list of attribute names if the query is asking about
   a specific attribute. Common ones: email, address, phone, role,
   employer, spouse, parent, child, birth_year, location, language,
   hobby, opinion. Omit if not predicate-shaped.
4. "rewrites": 3 to 4 alternate phrasings of the query:
   - Phrasing 1: declarative version (e.g. "Where does X live?" →
     "X lives in")
   - Phrasing 2: synonym substitution preserving meaning
   - Phrasing 3: ORTHOGONAL VOCABULARY — describe what the answer
     would look like, not what the question asks. (e.g. "What state
     did X visit?" → "X trip travel vacation photo location")
   - If the query is non-English, also include 1 English supplement.
5. "language_hint": ISO 639-1 code (en, es, zh, ja, ko, etc.) or "".

Output JSON ONLY, no commentary, no markdown fences.

EXAMPLES:

Query: "Where does Marcelo live?"
{{"intent":"factual_lookup","entities":["person:marcelo"],"predicates":["address","location"],"rewrites":["Marcelo lives in","Marcelo address residence city","Marcelo home location whereabouts"],"language_hint":"en"}}

Query: "马塞洛的邮箱是什么?"
{{"intent":"factual_lookup","entities":["person:marcelo"],"predicates":["email"],"rewrites":["马塞洛的邮箱地址","马塞洛 联系方式 邮件","Marcelo email address contact"],"language_hint":"zh"}}

USER QUERY: {query}
"""


# ---------------------------------------------------------------------------
# Parsing — tolerant to LLM JSON quirks
# ---------------------------------------------------------------------------


_FENCE_PATTERN = re.compile(
    r"^[`~]{3,}\s*(?:json|JSON)?\s*\n?(.*?)\n?[`~]{3,}\s*$",
    re.DOTALL,
)


def _strip_code_fences(raw: str) -> str:
    """Handle ```````, `````json``, ``~~~`` and mixed-case fences."""
    raw = raw.strip()
    m = _FENCE_PATTERN.match(raw)
    if m:
        return m.group(1).strip()
    # Inline fence not at line boundary: best-effort split.
    if raw.startswith("```"):
        parts = raw.split("```")
        if len(parts) >= 3:
            middle = parts[1]
            # Drop leading "json" tag if present
            middle = re.sub(r"^[Jj][Ss][Oo][Nn]\s*\n?", "", middle)
            return middle.strip()
    return raw


def _lenient_json_loads(raw: str) -> dict[str, Any]:
    """Parse JSON tolerantly. Uses ``json_repair`` when stdlib fails."""
    cleaned = _strip_code_fences(raw)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    try:
        import json_repair

        result = json_repair.loads(cleaned)
        if not isinstance(result, dict):
            return {}
        return result
    except Exception:  # noqa: BLE001
        return {}


def _passthrough(original_query: str) -> QueryRewrite:
    """Build a no-op QueryRewrite preserving the original query."""
    return QueryRewrite(
        intent="open_ended",
        entities=(),
        predicates=(),
        rewrites=(original_query,),
        language_hint="",
        used_llm=False,
    )


def _coerce_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for v in value:
        if isinstance(v, str) and v.strip():
            out.append(v.strip())
    return out


def _parse_response(raw: str, original_query: str) -> QueryRewrite:
    """Map LLM raw text into a QueryRewrite, falling back gracefully."""
    if not raw or not raw.strip():
        return _passthrough(original_query)

    data = _lenient_json_loads(raw)
    if not isinstance(data, dict) or not data:
        return _passthrough(original_query)

    # Always include the original query as the first rewrite (anchor —
    # preserves the original cosine ranking as a baseline lane in RRF).
    rewrites: list[str] = [original_query]
    for r in _coerce_str_list(data.get("rewrites")):
        if r != original_query and r not in rewrites:
            rewrites.append(r)
    rewrites = rewrites[:5]  # cap at original + 4 alternates

    return QueryRewrite(
        intent=str(data.get("intent") or "open_ended"),
        entities=tuple(_coerce_str_list(data.get("entities"))),
        predicates=tuple(_coerce_str_list(data.get("predicates"))),
        rewrites=tuple(rewrites),
        language_hint=str(data.get("language_hint") or ""),
        used_llm=True,
    )


# ---------------------------------------------------------------------------
# Cache primitives
# ---------------------------------------------------------------------------


def _cache_get(key: str) -> Optional[QueryRewrite]:
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry is None:
            return None
        ts, value = entry
        if time.time() - ts > _TTL_SECONDS:
            _CACHE.pop(key, None)
            return None
        return value


def _cache_set(key: str, value: QueryRewrite) -> None:
    with _CACHE_LOCK:
        if len(_CACHE) >= _MAX_CACHE_ENTRIES:
            # Drop the 20% oldest. O(N log N) eviction, but only fires
            # once we cross the bound and only every (0.2 * maxsize)
            # inserts thereafter — amortized fine for our usage.
            evict_count = max(1, _MAX_CACHE_ENTRIES // 5)
            oldest = sorted(_CACHE.items(), key=lambda kv: kv[1][0])[:evict_count]
            for k, _ in oldest:
                _CACHE.pop(k, None)
        _CACHE[key] = (time.time(), value)


def clear_cache() -> None:
    """Test-only: drop everything from the rewrite cache."""
    with _CACHE_LOCK:
        _CACHE.clear()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def rewrite_query_async(
    query: str,
    *,
    llm_invoke: Optional[Callable[..., str]] = None,
    model: str = "glm-4.5",
    use_cache: bool = True,
) -> QueryRewrite:
    """Async-safe variant for use inside ``memory_search.execute``.

    Args:
        query: User query string (any language).
        llm_invoke: Optional ``(prompt, *, model) -> str`` callable.
            Defaults to durin's z.ai-backed ``default_llm_invoke``.
            Tests pass stubs.
        model: LLM model id (must support multilingual; default
            ``glm-5.1`` covers EN/ES/ZH natively).
        use_cache: ``False`` bypasses cache (for tests + adversarial
            re-rewrites).

    Returns:
        :class:`QueryRewrite`. Never raises — LLM failures collapse to
        a passthrough with the original query.
    """
    if not query or not query.strip():
        return _passthrough(query)

    cache_key = _normalize_cache_key(query)
    if use_cache:
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

    if llm_invoke is None:
        try:
            from durin.memory.dream import default_llm_invoke
            llm_invoke = default_llm_invoke
        except Exception:  # noqa: BLE001
            logger.warning("rewrite: default_llm_invoke unavailable")
            return _passthrough(query)

    prompt = _PROMPT_TEMPLATE.format(query=query)
    # The invoke may be async (provider.chat_with_retry-style) or sync
    # (legacy default_llm_invoke + test stubs). Use the right awaitable
    # shape so async invokes don't get incorrectly thread-pooled and
    # sync invokes don't stall the event loop.
    is_async = inspect.iscoroutinefunction(llm_invoke)
    try:
        if is_async:
            raw = await asyncio.wait_for(
                llm_invoke(prompt, model=model),  # type: ignore[misc]
                timeout=_LLM_TIMEOUT_SECONDS,
            )
        else:
            raw = await asyncio.wait_for(
                asyncio.to_thread(llm_invoke, prompt, model=model),
                timeout=_LLM_TIMEOUT_SECONDS,
            )
    except asyncio.TimeoutError:
        logger.warning(
            "rewrite: LLM exceeded %.1fs — passthrough", _LLM_TIMEOUT_SECONDS,
        )
        return _passthrough(query)
    except Exception as exc:  # noqa: BLE001
        # Common: rate-limit, network, provider 5xx. Passthrough so
        # the caller sees no regression vs the no-rewrite baseline.
        logger.warning("rewrite: LLM call failed (%s) — passthrough", exc)
        return _passthrough(query)

    out = _parse_response(raw, query)
    if use_cache:
        _cache_set(cache_key, out)
    return out


def rewrite_query(
    query: str,
    *,
    llm_invoke: Optional[Callable[..., str]] = None,
    model: str = "glm-4.5",
    use_cache: bool = True,
) -> QueryRewrite:
    """Sync wrapper — runs the async path via ``asyncio.run``.

    Convenience for tests and CLI tools. Production callers inside the
    async tool loop should use :func:`rewrite_query_async` directly.
    """
    return asyncio.run(rewrite_query_async(
        query, llm_invoke=llm_invoke, model=model, use_cache=use_cache,
    ))


# ---------------------------------------------------------------------------
# Memory-model resolver (config.agents.aux_models.memory with fallback)
# ---------------------------------------------------------------------------


def resolve_memory_preset(config: Any) -> Any:
    """Pick the model preset memory LLM ops should use.

    Resolution order, falling through on each miss:

    1. ``config.agents.aux_models.memory.preset`` — named preset
       reference (uses the same resolution as vision/audio bridges).
    2. ``config.agents.aux_models.memory.model`` (+ optional
       ``provider``) — inline override; we synthesise a
       :class:`ModelPresetConfig` carrying just those fields plus
       provider defaults.
    3. The agent's active preset
       (``config.agents.defaults.model_preset``, or ``"default"``).
       This is the "no separate memory model configured → use what the
       agent uses" branch — keeps the user knob coherent unless they
       explicitly opt into a different model for memory ops.

    Returns:
        :class:`ModelPresetConfig`. Never ``None``: the active preset
        is always resolvable on a valid config.
    """
    from durin.config.schema import ModelPresetConfig

    aux = getattr(getattr(config, "agents", None), "aux_models", None)
    aux_memory = getattr(aux, "memory", None) if aux is not None else None
    if aux_memory is not None:
        if getattr(aux_memory, "preset", None):
            try:
                return config.resolve_preset(aux_memory.preset)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "memory aux preset %r missing — falling back to agent preset",
                    aux_memory.preset,
                )
        if getattr(aux_memory, "model", None):
            return ModelPresetConfig(
                model=aux_memory.model,
                provider=getattr(aux_memory, "provider", None) or "auto",
            )
    # Fallback: agent's active preset
    name = (
        getattr(getattr(config, "agents", None), "defaults", None)
        and config.agents.defaults.model_preset
    ) or "default"
    return config.resolve_preset(name)


def build_memory_llm_invoke(
    config: Any,
    *,
    aux_provider_handle: Any | None = None,
) -> tuple[AsyncLLMInvoke, str]:
    """Construct an async LLM invoke + the model name to use.

    The returned callable matches the ``async (prompt, *, model) -> str``
    shape that :func:`rewrite_query_async` accepts. It dispatches
    through durin's provider abstraction (``make_provider``) so the
    rewriter respects the configured provider/model combination —
    including providers other than the default z.ai (Anthropic,
    OpenAI, …) when the user has set ``aux_models.memory``.

    ``aux_provider_handle`` may be passed for the hot path: the
    AgentLoop pre-builds aux providers at startup (vision/audio/memory)
    and stashes them in :attr:`ToolContext.aux_providers`. Reusing that
    handle avoids re-running :func:`make_provider` on every search.
    When ``None``, we resolve + construct on demand.
    """
    from durin.providers.factory import make_provider

    preset = resolve_memory_preset(config)
    if aux_provider_handle is not None and getattr(aux_provider_handle, "provider", None):
        provider = aux_provider_handle.provider
        model_name = getattr(aux_provider_handle, "model", None) or preset.model
    else:
        provider = make_provider(config, preset=preset)
        model_name = preset.model

    async def _invoke(prompt: str, *, model: str | None = None) -> str:
        response = await provider.chat_with_retry(
            [{"role": "user", "content": prompt}],
            tools=None,
            model=model or model_name,
            max_tokens=512,
            temperature=0.2,
            retry_mode="standard",
        )
        return response.content or ""

    return _invoke, model_name
