"""Refresh ``durin/providers/data/model_capabilities.json`` from public sources.

Builds a single consensus file that ships with durin. The runtime
resolver reads only the consensus file — this script is a dev tool,
not part of the agent.

Source priority (May 2026)
--------------------------
**Tier 1 — Vendor APIs (authoritative)**. When the operator has set
the corresponding API key in the environment, the script hits the
vendor's own ``/models`` endpoint and treats the result as ground
truth. Vendor data OVERWRITES community-merge values field by field
(vendor wins) instead of being OR-merged. See ``scripts/_vendor_sources.py``
for which vendors are wired and what they actually expose.

Currently wired:

- **Anthropic** (`ANTHROPIC_API_KEY`) → rich: per-model
  ``capabilities.{image_input, pdf_input, structured_outputs, thinking,
  …}`` + ``max_input_tokens`` + ``max_tokens``.
- **Mistral** (`MISTRAL_API_KEY`) → rich: ``capabilities.{vision,
  function_calling, …}`` + ``max_context_length`` + aliases.
- **Google Gemini** (`GEMINI_API_KEY` or `GOOGLE_API_KEY`) → decent:
  ``inputTokenLimit`` + ``outputTokenLimit`` + ``supportedGenerationMethods``
  + ``thinking``.

**Tier 2 — Community merge (consensus fallback)**:

1. **LiteLLM** ``model_prices_and_context_window.json`` — curated by
   BerriAI; broad coverage of frontier + gateway providers.
2. **OpenRouter** ``/api/v1/models`` — input modality taxonomy with
   reliable vision/image/audio/video flags.
3. **models.dev** ``/api.json`` — community-curated; deep coverage of
   chinese-region providers (Zhipu/zai, etc.) that LiteLLM under-covers.

Merge rules
-----------
- **Canonical key**: bare model name (everything after the last ``/``
  or ``.`` provider prefix), lowercased. Multiple entries with the
  same canonical key merge into one record.
- **Phase 1 — community merge**. Booleans OR (any source affirming a
  capability wins; sources rarely fabricate, the failure mode is
  omitting). Numerics take MAX. Mode keeps the first non-default.
- **Phase 2 — vendor override**. For every field the vendor *explicitly*
  asserted (sparse dict — fields the vendor doesn't mention stay
  whatever the community merge produced), overwrite. ``_authority`` is
  set to ``"vendor"`` for any model touched by a vendor adapter, ``"merge"``
  otherwise.
- **``_sources``**: list of ``<source>:<original-key>`` strings showing
  exactly which entries from which sources fed each record.
- **``_vendor_sources``**: subset of ``_sources`` that came from vendor
  APIs — handy for verifying "is this entry authoritative or just
  community-curated".

Usage
-----
    python scripts/refresh_model_capabilities.py
        # → writes durin/providers/data/model_capabilities.json

    python scripts/refresh_model_capabilities.py --dry-run
        # → fetch + merge but don't write; prints summary

The script needs network access (it hits multiple HTTPS endpoints). If
a source fails, it falls back to whatever data it has and warns on
stderr. The output file is intentionally checked in so the runtime
works offline; refresh on demand.

Vendor adapters are **opt-in**: missing API keys are silent (logged in
the summary, no failure). CI without vendor keys still produces a
valid snapshot from community sources alone — same behaviour as before
the vendor-adapter work.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import httpx

# Vendor-API adapters (Tier 1 source of truth). Lives in a sibling
# module so each vendor's HTTP / parsing concerns stay isolated.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _vendor_sources import iter_vendor_streams  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = REPO_ROOT / "durin" / "providers" / "data" / "model_capabilities.json"

LITELLM_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
OPENROUTER_URL = "https://openrouter.ai/api/v1/models"
MODELS_DEV_URL = "https://models.dev/api.json"

# Prefixes we strip from source keys to compute the canonical bare name.
# Order matters: we strip provider/gateway segments greedily then handle
# the bedrock-style ``provider.model`` form.
_KNOWN_PROVIDER_DOTS = frozenset({
    "anthropic", "amazon", "meta", "mistral", "cohere", "ai21",
    "stability", "openai", "qwen", "deepseek", "moonshot", "zai",
})

# First-party publishers — only entries whose source-provider matches
# one of these are accepted into the merge. Aggregators, inference
# gateways, and routing services are deliberately excluded so they
# cannot pollute capability flags. For example, ``302ai/glm-5v-turbo``
# erroneously declares ``audio`` for a Zhipu model that doesn't support
# it; an OR-merge across all sources would silently propagate that
# error. Restricting to vendor-originals keeps the consensus honest.
TRUSTED_VENDORS = frozenset({
    "anthropic",
    "openai",
    "google", "gemini",
    "zai", "zhipuai", "z-ai", "zai-org",
    "meta", "meta-llama",
    "mistral", "mistralai",
    "deepseek",
    "xai",
    "qwen", "alibaba", "qwen3",
    "moonshot",
    "amazon",
    "cohere",
    "minimax", "minimax-anthropic",
    "stepfun",
    "ai21",
    "ibm",
    "01-ai",
    "databricks",
    "nvidia",
    "voyage", "voyage-ai",
    "perplexity",
    "writer",
    "cerebras",  # publishes its own native models alongside hosting; close-enough vendor
})


def _canonical_key(raw: str) -> str:
    """Return the bare lowercase model id used as the consensus key.

    Strips gateway and provider prefixes (everything before the last
    ``/``), then strips a leading ``<provider>.`` prefix when the
    provider is a known dotted-notation publisher (bedrock-style).
    """
    if not raw:
        return ""
    key = raw.strip()
    if "/" in key:
        key = key.rsplit("/", 1)[1]
    if "." in key:
        head, sep, rest = key.partition(".")
        if head.lower() in _KNOWN_PROVIDER_DOTS and rest:
            key = rest
    return key.lower()


# ---------------------------------------------------------------------------
# Source adapters
# ---------------------------------------------------------------------------


def _fetch_json(url: str, *, timeout: float = 30.0) -> Any:
    print(f"  → GET {url}", file=sys.stderr)
    r = httpx.get(url, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _from_litellm(data: dict[str, Any]) -> Iterable[tuple[str, str, dict[str, Any]]]:
    """Yield ``(canonical_key, source_label, capability_dict)`` tuples.

    Skips entries whose ``litellm_provider`` is not in TRUSTED_VENDORS
    (gateways, inference routers, aggregators). The remaining entries
    are vendor-published metadata we can trust to OR-merge.
    """
    for raw_key, entry in data.items():
        if raw_key == "sample_spec" or not isinstance(entry, dict):
            continue
        provider = (entry.get("litellm_provider") or "").lower()
        if provider not in TRUSTED_VENDORS:
            continue
        canon = _canonical_key(raw_key)
        if not canon:
            continue
        supported_in = entry.get("supported_modalities") or []
        supported_out = entry.get("supported_output_modalities") or []
        caps = {
            "max_input_tokens": entry.get("max_input_tokens") or entry.get("max_tokens"),
            "max_output_tokens": entry.get("max_output_tokens") or entry.get("max_tokens"),
            "mode": entry.get("mode") or "chat",
            "supports_vision": bool(entry.get("supports_vision") or "image" in supported_in),
            "supports_audio_input": bool(entry.get("supports_audio_input") or "audio" in supported_in),
            "supports_pdf_input": bool(entry.get("supports_pdf_input")),
            "supports_video_input": "video" in supported_in,
            "supports_audio_output": bool(entry.get("supports_audio_output") or "audio" in supported_out),
            "supports_image_output": "image" in supported_out,
            "supports_function_calling": bool(entry.get("supports_function_calling")),
            "supports_reasoning": bool(entry.get("supports_reasoning")),
            "supports_prompt_caching": bool(entry.get("supports_prompt_caching")),
            "supports_response_schema": bool(entry.get("supports_response_schema")),
        }
        yield canon, f"litellm:{raw_key}", caps


def _from_openrouter(data: dict[str, Any]) -> Iterable[tuple[str, str, dict[str, Any]]]:
    """OpenRouter's /api/v1/models response (list under ``data``).

    Schema: each entry has ``id`` (slug), ``context_length``,
    ``architecture.input_modalities`` (list of strings: text, image,
    file, audio, video), and ``supported_parameters`` (list including
    'tools' when the model supports function calling).
    """
    for entry in data.get("data", []):
        raw_key = entry.get("id") or ""
        # OpenRouter ids are ``<vendor-slug>/<model>``; skip anything
        # whose vendor isn't on the trusted list (effectively excludes
        # OpenRouter's own routing aliases and other gateway slugs).
        vendor = raw_key.split("/", 1)[0].lower() if "/" in raw_key else ""
        if vendor not in TRUSTED_VENDORS:
            continue
        canon = _canonical_key(raw_key)
        if not canon:
            continue
        arch = entry.get("architecture") or {}
        in_mods = set(arch.get("input_modalities") or [])
        out_mods = set(arch.get("output_modalities") or [])
        params = set(entry.get("supported_parameters") or [])
        caps = {
            "max_input_tokens": entry.get("context_length"),
            # OpenRouter doesn't separate input/output context; reuse.
            "max_output_tokens": entry.get("top_provider", {}).get("max_completion_tokens"),
            "mode": "chat",  # OpenRouter only lists chat-style models
            "supports_vision": "image" in in_mods,
            "supports_audio_input": "audio" in in_mods,
            "supports_pdf_input": "file" in in_mods,
            "supports_video_input": "video" in in_mods,
            "supports_audio_output": "audio" in out_mods,
            "supports_image_output": "image" in out_mods,
            "supports_function_calling": "tools" in params or "tool_choice" in params,
            "supports_reasoning": "reasoning" in params or "reasoning_effort" in params,
            "supports_prompt_caching": False,  # OpenRouter doesn't expose this
            "supports_response_schema": "response_format" in params or "structured_outputs" in params,
        }
        yield canon, f"openrouter:{raw_key}", caps


def _from_models_dev(data: dict[str, Any]) -> Iterable[tuple[str, str, dict[str, Any]]]:
    """models.dev/api.json is a dict of provider → {models: {id: meta}}.

    Each model has ``modalities.input`` / ``modalities.output``
    (canonical: text/image/video/audio/pdf), ``limit.context``,
    ``limit.output``, optional ``tool_call`` and ``reasoning`` flags.
    """
    for provider_id, prov in data.items():
        # Reject aggregator providers (kilo/, vercel/, 302ai/, etc.).
        # Only first-party vendor publications contribute capabilities.
        if provider_id.lower() not in TRUSTED_VENDORS:
            continue
        models = (prov or {}).get("models") or {}
        for raw_id, m in models.items():
            if not isinstance(m, dict):
                continue
            raw_key = f"{provider_id}/{raw_id}"
            canon = _canonical_key(raw_key)
            if not canon:
                continue
            mods = m.get("modalities") or {}
            in_mods = set(mods.get("input") or [])
            out_mods = set(mods.get("output") or [])
            limits = m.get("limit") or {}
            caps = {
                "max_input_tokens": limits.get("context"),
                "max_output_tokens": limits.get("output"),
                "mode": "chat",  # models.dev mostly tracks chat models
                "supports_vision": "image" in in_mods,
                "supports_audio_input": "audio" in in_mods,
                "supports_pdf_input": "pdf" in in_mods,
                "supports_video_input": "video" in in_mods,
                "supports_audio_output": "audio" in out_mods,
                "supports_image_output": "image" in out_mods,
                "supports_function_calling": bool(m.get("tool_call")),
                "supports_reasoning": bool(m.get("reasoning")),
                "supports_prompt_caching": False,
                "supports_response_schema": False,
            }
            yield canon, f"models.dev:{raw_key}", caps


# ---------------------------------------------------------------------------
# Merge logic
# ---------------------------------------------------------------------------


_BOOL_FIELDS = (
    "supports_vision",
    "supports_audio_input",
    "supports_pdf_input",
    "supports_video_input",
    "supports_audio_output",
    "supports_image_output",
    "supports_function_calling",
    "supports_reasoning",
    "supports_prompt_caching",
    "supports_response_schema",
)
_NUMERIC_FIELDS = ("max_input_tokens", "max_output_tokens")


def _merge(into: dict[str, Any], incoming: dict[str, Any]) -> None:
    """Merge *incoming* capability dict into *into* in place.

    Booleans use OR — once True, stays True. Numerics take MAX of the
    two values (None < int). ``mode`` keeps the first non-default
    value seen (sources rarely disagree here).
    """
    for f in _BOOL_FIELDS:
        if incoming.get(f):
            into[f] = True
    for f in _NUMERIC_FIELDS:
        a, b = into.get(f), incoming.get(f)
        if a is None:
            into[f] = b
        elif b is not None:
            into[f] = max(a, b)
    if into.get("mode", "chat") == "chat" and incoming.get("mode") not in (None, "chat"):
        into["mode"] = incoming["mode"]


def consolidate(
    litellm: dict[str, Any] | None,
    openrouter: dict[str, Any] | None,
    models_dev: dict[str, Any] | None,
    vendor_streams: list[Iterable[tuple[str, str, dict[str, Any]]]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Merge community sources + apply vendor-API overrides.

    Phase 1 — community merge (OR for bools, MAX for numerics): same as
    before. Phase 2 — vendor overlay: for every field a vendor adapter
    explicitly asserted, overwrite the merged value. Vendor-sourced
    entries are tagged ``_authority="vendor"``; everything else stays
    ``_authority="merge"``.

    ``vendor_streams`` accepts a pre-materialised list (the CLI builds
    it via ``_vendor_sources.iter_vendor_streams``). Tests can pass
    custom streams directly. ``None`` means "no vendor data" and the
    function behaves identically to the pre-vendor version.
    """
    out: dict[str, dict[str, Any]] = {}

    streams: list[Iterable[tuple[str, str, dict[str, Any]]]] = []
    if litellm:
        streams.append(_from_litellm(litellm))
    if openrouter:
        streams.append(_from_openrouter(openrouter))
    if models_dev:
        streams.append(_from_models_dev(models_dev))

    for stream in streams:
        for canon, src_label, caps in stream:
            entry = out.setdefault(canon, _empty_entry())
            _merge(entry, caps)
            entry["_sources"].append(src_label)

    # Phase 2: vendor overlay. Sparse — only fields the vendor
    # explicitly answered overwrite the merged values. New canonical
    # keys discovered by vendors (not in any community source) are
    # added with _authority="vendor".
    for stream in vendor_streams or []:
        for canon, src_label, caps in stream:
            entry = out.setdefault(canon, _empty_entry())
            for field, value in caps.items():
                entry[field] = value
            entry["_sources"].append(src_label)
            entry.setdefault("_vendor_sources", []).append(src_label)
            entry["_authority"] = "vendor"

    # Tag the leftover (no vendor touched them) as merge-authority.
    for entry in out.values():
        entry.setdefault("_authority", "merge")

    return out


def _empty_entry() -> dict[str, Any]:
    """Fresh capability record with safe defaults. Booleans default to
    False so an entry seen only as a name (e.g. vendor listed it but
    asserted no capabilities) still has consistent shape."""
    return {
        "max_input_tokens": None,
        "max_output_tokens": None,
        "mode": "chat",
        **{f: False for f in _BOOL_FIELDS},
        "_sources": [],
    }


def build_consensus_file(
    litellm: dict[str, Any] | None,
    openrouter: dict[str, Any] | None,
    models_dev: dict[str, Any] | None,
    vendor_streams: list[Iterable[tuple[str, str, dict[str, Any]]]] | None = None,
    vendor_attempted: list[str] | None = None,
    vendor_skipped: list[str] | None = None,
) -> dict[str, Any]:
    """Wrap the merged models dict in the on-disk schema."""
    models = consolidate(litellm, openrouter, models_dev, vendor_streams=vendor_streams)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return {
        # v2: vendor-source provenance added. Backward-compatible with
        # the runtime resolver — extra fields are ignored.
        "schema_version": 2,
        "generated_at": now,
        "sources": {
            "litellm": {"url": LITELLM_URL, "present": litellm is not None},
            "openrouter": {"url": OPENROUTER_URL, "present": openrouter is not None},
            "models.dev": {"url": MODELS_DEV_URL, "present": models_dev is not None},
        },
        "vendor_sources": {
            "attempted": list(vendor_attempted or []),
            "skipped": list(vendor_skipped or []),
        },
        "models": models,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="don't write the output file")
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH, help="output JSON path")
    args = parser.parse_args()

    sources: dict[str, Any] = {"litellm": None, "openrouter": None, "models.dev": None}
    print("Fetching community sources…", file=sys.stderr)
    for name, url in (
        ("litellm", LITELLM_URL),
        ("openrouter", OPENROUTER_URL),
        ("models.dev", MODELS_DEV_URL),
    ):
        try:
            sources[name] = _fetch_json(url)
        except Exception as exc:
            print(f"  ! {name} failed: {exc}", file=sys.stderr)

    print("\nFetching vendor sources (when API keys are present)…", file=sys.stderr)
    vendor_streams, vendor_attempted, vendor_skipped = iter_vendor_streams(_canonical_key)
    if not vendor_attempted:
        print("  (no vendor API keys set — relying on community merge alone)",
              file=sys.stderr)

    payload = build_consensus_file(
        sources["litellm"],
        sources["openrouter"],
        sources["models.dev"],
        vendor_streams=vendor_streams,
        vendor_attempted=vendor_attempted,
        vendor_skipped=vendor_skipped,
    )

    models = payload["models"]
    community_present = sum(1 for s in payload["sources"].values() if s["present"])
    vendor_present = len(vendor_attempted)
    print(f"\nMerged {len(models)} canonical models from "
          f"{community_present}/3 community sources + "
          f"{vendor_present} vendor API(s).", file=sys.stderr)

    # Provenance split: how many entries were vendor-authoritative vs
    # merge-only? Lets the operator see at a glance whether the vendor
    # adapters actually contributed.
    by_authority = Counter(entry.get("_authority", "merge") for entry in models.values())
    print("Authority split:", file=sys.stderr)
    for label, count in by_authority.most_common():
        print(f"  {label}: {count}", file=sys.stderr)

    # Summary: how many models advertise each modality
    counts = Counter()
    for caps in models.values():
        for f in _BOOL_FIELDS:
            if caps.get(f):
                counts[f] += 1
    print("Capability counts:", file=sys.stderr)
    for f, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {f}: {n}", file=sys.stderr)

    # Spotlight: did we catch glm-5v-turbo correctly?
    sample = models.get("glm-5v-turbo")
    if sample:
        print("\nSanity sample — glm-5v-turbo:", file=sys.stderr)
        print(f"  vision={sample['supports_vision']}  audio={sample['supports_audio_input']} "
              f"pdf={sample['supports_pdf_input']}  video={sample['supports_video_input']}", file=sys.stderr)
        print(f"  authority={sample.get('_authority')}", file=sys.stderr)
        print(f"  sources: {len(sample['_sources'])} ({sample['_sources'][:3]}…)", file=sys.stderr)

    # Vendor-skipped log (lets the operator know which vendor adapters
    # were silently disabled vs failed mid-call).
    if vendor_skipped:
        print("\nVendor adapters skipped:", file=sys.stderr)
        for line in vendor_skipped:
            print(f"  - {line}", file=sys.stderr)

    if args.dry_run:
        print("\n--dry-run: not writing output.", file=sys.stderr)
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nWrote {args.output} ({args.output.stat().st_size // 1024} KB).", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
