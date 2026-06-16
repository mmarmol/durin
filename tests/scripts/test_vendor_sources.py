"""Tests for the vendor-API adapters in ``scripts/_vendor_sources.py``.

The refresh script's previous 3 community sources (LiteLLM, OpenRouter,
models.dev) treat capability data as OR-mergeable hints from sources of
roughly equal trust. Vendor adapters added in May 2026 are a separate
tier: when present, they're authoritative — their explicit assertions
overwrite community-merge values.

These tests cover:

- Each adapter's per-entry parser (sparse output: only fields the
  vendor asserted; nothing defaulted to ``False`` or ``None``).
- The ``iter_vendor_streams`` dispatcher (silent skip when keys absent,
  recovery from network failure, recovery from parse failure).
- Integration with ``consolidate()``: vendor data overrides community
  for the same canonical key; new keys discovered by vendor only are
  added with ``_authority="vendor"``; everything else stays
  ``_authority="merge"``.

The HTTP layer is monkey-patched — no test makes a real network call.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"


def _load_module(name: str, fname: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS_DIR / fname)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def vsrc():
    """Loaded _vendor_sources module."""
    return _load_module("_vsrc", "_vendor_sources.py")


@pytest.fixture(scope="module")
def refresh():
    """Loaded refresh_model_capabilities module."""
    # _vendor_sources must be on sys.path for the refresh script's
    # in-script import to succeed.
    import sys
    sys.path.insert(0, str(_SCRIPTS_DIR))
    return _load_module("_refresh", "refresh_model_capabilities.py")


# ---------------------------------------------------------------------------
# Anthropic adapter
# ---------------------------------------------------------------------------


def test_anthropic_extracts_capabilities_sparsely(vsrc):
    """Only fields Anthropic *explicitly* answered appear in the output
    sparse dict. Unanswered fields stay absent so the community merge
    can fill them in."""
    payload = {
        "data": [
            {
                "id": "claude-3-5-sonnet-20240620",
                "type": "model",
                "max_input_tokens": 200_000,
                "max_tokens": 8192,
                "capabilities": {
                    "image_input": {"supported": True},
                    "pdf_input": {"supported": True},
                    "thinking": {"supported": False},
                    "structured_outputs": {"supported": True},
                    # citations/batch/etc deliberately omitted — adapter
                    # must not invent them.
                },
            },
        ],
    }
    entries = list(vsrc._from_anthropic(payload, lambda s: s.lower()))
    assert len(entries) == 1
    canon, label, caps = entries[0]
    assert canon == "claude-3-5-sonnet-20240620"
    assert label.startswith("vendor:anthropic/")
    assert caps == {
        "max_input_tokens": 200_000,
        "max_output_tokens": 8192,
        "supports_vision": True,
        "supports_pdf_input": True,
        "supports_reasoning": False,
        "supports_response_schema": True,
    }


def test_anthropic_handles_missing_capabilities_block(vsrc):
    """Older Anthropic API response shapes (or models in beta) may not
    include the ``capabilities`` block at all. Adapter must not crash;
    it just yields the numeric fields that ARE present."""
    payload = {"data": [{"id": "claude-future", "max_input_tokens": 100_000}]}
    entries = list(vsrc._from_anthropic(payload, lambda s: s.lower()))
    assert len(entries) == 1
    canon, _, caps = entries[0]
    assert canon == "claude-future"
    assert caps == {"max_input_tokens": 100_000}


def test_anthropic_handles_empty_data(vsrc):
    """Anthropic returns ``data: []`` when no models match the filter
    (or, hypothetically, when the account has no enabled models). No
    crash, no yields."""
    assert list(vsrc._from_anthropic({}, lambda s: s.lower())) == []
    assert list(vsrc._from_anthropic({"data": []}, lambda s: s.lower())) == []


def test_anthropic_handles_garbage_entries(vsrc):
    """Robustness: a non-dict entry inside ``data`` must be skipped,
    not crash the parser."""
    payload = {"data": [None, "garbage", {"id": "claude-real", "max_tokens": 1000}]}
    entries = list(vsrc._from_anthropic(payload, lambda s: s.lower()))
    assert len(entries) == 1
    assert entries[0][0] == "claude-real"


# ---------------------------------------------------------------------------
# Mistral adapter
# ---------------------------------------------------------------------------


def test_mistral_extracts_capabilities(vsrc):
    payload = {
        "data": [
            {
                "id": "mistral-large-2407",
                "max_context_length": 131_072,
                "capabilities": {
                    "completion_chat": True,
                    "function_calling": True,
                    "vision": False,
                    "fine_tuning": False,
                },
            },
        ],
    }
    entries = list(vsrc._from_mistral(payload, lambda s: s.lower()))
    assert len(entries) == 1
    canon, _, caps = entries[0]
    assert canon == "mistral-large-2407"
    assert caps == {
        "max_input_tokens": 131_072,
        "supports_vision": False,
        "supports_function_calling": True,
    }


def test_mistral_emits_aliases_with_same_caps(vsrc):
    """Mistral's ``aliases`` list (e.g. ``mistral-large-latest`` ←
    ``mistral-large-2407``) should each get their own entry under the
    alias canonical key with identical caps."""
    payload = {
        "data": [
            {
                "id": "mistral-large-2407",
                "max_context_length": 131_072,
                "aliases": ["mistral-large-latest"],
                "capabilities": {"vision": False, "function_calling": True},
            },
        ],
    }
    entries = list(vsrc._from_mistral(payload, lambda s: s.lower()))
    canons = [e[0] for e in entries]
    assert canons == ["mistral-large-2407", "mistral-large-latest"]
    # Both entries have the same capability dict by reference (identical content).
    assert entries[0][2] == entries[1][2]
    # Source label distinguishes them so debuggability survives.
    assert "#alias:" in entries[1][1]


def test_mistral_skips_deprecated_models(vsrc):
    """Mistral marks deprecated models with a ``deprecation`` timestamp.
    Including them in the snapshot would surface dead models to users."""
    payload = {
        "data": [
            {
                "id": "mistral-medium-deprecated",
                "deprecation": "2024-06-01",
                "max_context_length": 32_000,
                "capabilities": {"completion_chat": True},
            },
            {
                "id": "mistral-large-2407",
                "max_context_length": 131_072,
                "capabilities": {"completion_chat": True},
            },
        ],
    }
    entries = list(vsrc._from_mistral(payload, lambda s: s.lower()))
    canons = [e[0] for e in entries]
    assert canons == ["mistral-large-2407"]


# ---------------------------------------------------------------------------
# Gemini adapter
# ---------------------------------------------------------------------------


def test_gemini_extracts_token_limits_and_thinking(vsrc):
    payload = {
        "models": [
            {
                "name": "models/gemini-2.5-flash",
                "inputTokenLimit": 1_048_576,
                "outputTokenLimit": 65_536,
                "supportedGenerationMethods": ["generateContent", "streamGenerateContent"],
                "thinking": True,
            },
        ],
    }
    entries = list(vsrc._from_gemini(payload, lambda s: s.lower()))
    assert len(entries) == 1
    canon, _, caps = entries[0]
    assert canon == "gemini-2.5-flash"
    assert caps == {
        "max_input_tokens": 1_048_576,
        "max_output_tokens": 65_536,
        "supports_reasoning": True,
    }


def test_gemini_skips_non_chat_models(vsrc):
    """Models that don't support generateContent (e.g. embedding-only
    or fine-tuning-only) shouldn't end up in our chat snapshot."""
    payload = {
        "models": [
            {
                "name": "models/embedding-001",
                "inputTokenLimit": 2048,
                "supportedGenerationMethods": ["embedContent"],
            },
            {
                "name": "models/gemini-2.5-pro",
                "inputTokenLimit": 2_097_152,
                "supportedGenerationMethods": ["generateContent"],
            },
        ],
    }
    entries = list(vsrc._from_gemini(payload, lambda s: s.lower()))
    canons = [e[0] for e in entries]
    assert canons == ["gemini-2.5-pro"]


# ---------------------------------------------------------------------------
# Dispatcher: iter_vendor_streams
# ---------------------------------------------------------------------------


def test_iter_vendor_streams_skips_silently_when_no_keys(vsrc, monkeypatch):
    """Without any vendor API key set, every adapter must skip
    silently — no exceptions, no streams, just a skipped log entry."""
    for var in ("ANTHROPIC_API_KEY", "MISTRAL_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    streams, attempted, skipped = vsrc.iter_vendor_streams(lambda s: s.lower())
    assert streams == []
    assert attempted == []
    # Every adapter appears in skipped with a "no API key" reason.
    assert len(skipped) == 3
    assert all("no " in line and "_API_KEY" in line for line in skipped)


def test_iter_vendor_streams_recovers_from_fetch_failure(vsrc, monkeypatch):
    """A vendor whose API returns a 500 (or network error) must NOT
    abort the refresh — it's logged as skipped, other adapters and the
    community merge proceed."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    monkeypatch.setenv("MISTRAL_API_KEY", "sk-fake")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    def explode(_key):
        raise RuntimeError("503 Service Unavailable")

    # Patch the module-level fetcher refs; the dispatcher calls them.
    monkeypatch.setattr(vsrc, "_fetch_anthropic", explode)
    # Mistral succeeds with empty data.
    monkeypatch.setattr(vsrc, "_fetch_mistral", lambda _key: {"data": []})

    streams, attempted, skipped = vsrc.iter_vendor_streams(lambda s: s.lower())
    assert "mistral" in attempted
    assert "anthropic" not in attempted
    assert any("anthropic" in s and "503" in s for s in skipped)


def test_iter_vendor_streams_recovers_from_parse_failure(vsrc, monkeypatch):
    """If the vendor returns a payload that doesn't match the adapter's
    expected shape, the parser raises — the dispatcher must catch it
    and exclude that vendor from the run, not crash the whole refresh."""
    monkeypatch.setenv("MISTRAL_API_KEY", "sk-fake")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    monkeypatch.setattr(vsrc, "_fetch_mistral", lambda _key: {"data": [{"x": "y"}]})
    # Force the iterator to raise mid-parse.
    def explode(_data, _canon):
        raise ValueError("parse went wrong")
        yield  # unreachable — keeps it a generator
    monkeypatch.setattr(vsrc, "_from_mistral", explode)

    streams, attempted, skipped = vsrc.iter_vendor_streams(lambda s: s.lower())
    assert attempted == []
    assert any("mistral" in s and "parse failed" in s for s in skipped)


def test_iter_vendor_streams_accepts_google_api_key_alias(vsrc, monkeypatch):
    """Gemini docs and SDK accept ``GEMINI_API_KEY`` *or* the older
    ``GOOGLE_API_KEY``. Either must work, neither required."""
    for var in ("ANTHROPIC_API_KEY", "MISTRAL_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "fake")
    monkeypatch.setattr(vsrc, "_fetch_gemini", lambda _key: {"models": []})

    streams, attempted, skipped = vsrc.iter_vendor_streams(lambda s: s.lower())
    assert "gemini" in attempted


# ---------------------------------------------------------------------------
# Integration with consolidate()
# ---------------------------------------------------------------------------


def test_consolidate_vendor_overrides_community(refresh):
    """The headline contract: when community merge says X for a field
    and vendor adapter says Y for the same field on the same model,
    Y wins."""
    fake_litellm = {
        "claude-sample": {
            "litellm_provider": "anthropic",
            "max_input_tokens": 100_000,         # community says 100K
            "supports_pdf_input": False,          # community WRONG
        },
    }
    vendor_stream = [
        ("claude-sample", "vendor:anthropic/claude-sample", {
            "max_input_tokens": 200_000,          # vendor says 200K
            "supports_pdf_input": True,            # vendor AUTHORITATIVE
        }),
    ]
    out = refresh.consolidate(fake_litellm, None, None, vendor_streams=[vendor_stream])
    entry = out["claude-sample"]
    assert entry["max_input_tokens"] == 200_000
    assert entry["supports_pdf_input"] is True
    assert entry["_authority"] == "vendor"
    assert "vendor:anthropic/claude-sample" in entry["_vendor_sources"]


def test_consolidate_vendor_silence_keeps_community(refresh):
    """When the vendor adapter is silent about a field (sparse dict),
    the community merge value survives — vendor only overrides where
    it explicitly answered."""
    fake_litellm = {
        "claude-sample": {
            "litellm_provider": "anthropic",
            "supports_vision": True,              # community says True
            "supports_pdf_input": False,          # community says False
        },
    }
    # Vendor says nothing about vision; only asserts pdf_input.
    vendor_stream = [
        ("claude-sample", "vendor:anthropic/claude-sample", {
            "supports_pdf_input": True,
        }),
    ]
    out = refresh.consolidate(fake_litellm, None, None, vendor_streams=[vendor_stream])
    entry = out["claude-sample"]
    assert entry["supports_vision"] is True        # community survived
    assert entry["supports_pdf_input"] is True     # vendor wrote
    assert entry["_authority"] == "vendor"


def test_consolidate_vendor_only_model_creates_entry(refresh):
    """A model that EXISTS only in vendor data (not in any community
    source) still ends up in the snapshot, tagged as vendor-authority."""
    vendor_stream = [
        ("brand-new-model", "vendor:anthropic/brand-new", {
            "max_input_tokens": 500_000,
            "supports_vision": True,
        }),
    ]
    out = refresh.consolidate(None, None, None, vendor_streams=[vendor_stream])
    assert "brand-new-model" in out
    assert out["brand-new-model"]["_authority"] == "vendor"


def test_consolidate_authority_label_is_merge_when_no_vendor_touched(refresh):
    """Community-only models keep ``_authority="merge"`` so callers can
    distinguish authoritative from consensus entries."""
    fake_litellm = {
        "openai-model": {
            "litellm_provider": "openai",
            "max_input_tokens": 8000,
        },
    }
    out = refresh.consolidate(fake_litellm, None, None, vendor_streams=[])
    assert out["openai-model"]["_authority"] == "merge"
    assert "_vendor_sources" not in out["openai-model"]


def test_build_consensus_file_schema_v2(refresh):
    """Snapshot envelope: ``schema_version`` bumped to 2,
    ``vendor_sources`` block present with attempted + skipped lists."""
    payload = refresh.build_consensus_file(
        None, None, None,
        vendor_streams=[],
        vendor_attempted=["anthropic"],
        vendor_skipped=["mistral: no MISTRAL_API_KEY in env"],
    )
    assert payload["schema_version"] == 2
    assert payload["vendor_sources"] == {
        "attempted": ["anthropic"],
        "skipped": ["mistral: no MISTRAL_API_KEY in env"],
    }


def test_build_consensus_file_backward_compat_when_no_vendor_args(refresh):
    """A caller that doesn't pass vendor args still gets a valid v2
    envelope (no crash, empty vendor block)."""
    payload = refresh.build_consensus_file(None, None, None)
    assert payload["schema_version"] == 2
    assert payload["vendor_sources"] == {"attempted": [], "skipped": []}
