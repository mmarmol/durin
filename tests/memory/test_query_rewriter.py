"""Unit tests for G3.b query rewriter."""

from __future__ import annotations

import json
from typing import Any

import pytest

from durin.memory.query_rewriter import (
    QueryRewrite,
    _fullwidth_to_halfwidth,
    _lenient_json_loads,
    _normalize_cache_key,
    _parse_response,
    _strip_code_fences,
    clear_cache,
    rewrite_query,
)


# ---------------------------------------------------------------------------
# Pure helpers — no LLM, no cache
# ---------------------------------------------------------------------------


class TestFullwidthConversion:
    def test_converts_fullwidth_latin_to_halfwidth(self) -> None:
        assert _fullwidth_to_halfwidth("Ｍarcelo") == "Marcelo"
        assert _fullwidth_to_halfwidth("ＡＢＣ１２３") == "ABC123"

    def test_converts_fullwidth_space(self) -> None:
        assert _fullwidth_to_halfwidth("a　b") == "a b"

    def test_preserves_cjk_characters(self) -> None:
        assert _fullwidth_to_halfwidth("马塞洛") == "马塞洛"
        assert _fullwidth_to_halfwidth("キャロライン") == "キャロライン"

    def test_preserves_halfwidth_katakana(self) -> None:
        # Half-width katakana (U+FF65–FF9F) is legitimate Japanese usage,
        # must NOT be converted.
        assert _fullwidth_to_halfwidth("ｱｲｳ") == "ｱｲｳ"


class TestCacheKeyNormalization:
    def test_nfc_normalization(self) -> None:
        decomposed = "Marceló"  # M-a-r-c-e-l-o + combining acute
        precomposed = "Marceló"
        assert _normalize_cache_key(decomposed) == _normalize_cache_key(precomposed)

    def test_whitespace_collapse(self) -> None:
        assert _normalize_cache_key("a   b\t\tc") == "a b c"

    def test_lowercases_latin(self) -> None:
        assert _normalize_cache_key("Marcelo") == "marcelo"

    def test_strips_leading_trailing(self) -> None:
        assert _normalize_cache_key("  Marcelo  ") == "marcelo"

    def test_fullwidth_normalized_in_key(self) -> None:
        assert _normalize_cache_key("Ｍarcelo") == "marcelo"

    def test_cjk_unchanged_lowercase_safe(self) -> None:
        assert _normalize_cache_key("马塞洛") == "马塞洛"


class TestFenceStripping:
    def test_triple_backtick_json_fence(self) -> None:
        raw = "```json\n{\"a\": 1}\n```"
        assert _strip_code_fences(raw) == '{"a": 1}'

    def test_triple_backtick_no_lang_tag(self) -> None:
        raw = "```\n{\"a\": 1}\n```"
        assert _strip_code_fences(raw) == '{"a": 1}'

    def test_tilde_fence(self) -> None:
        raw = "~~~json\n{\"a\": 1}\n~~~"
        assert _strip_code_fences(raw) == '{"a": 1}'

    def test_no_fence_passes_through(self) -> None:
        raw = '{"a": 1}'
        assert _strip_code_fences(raw) == '{"a": 1}'

    def test_uppercase_lang_tag(self) -> None:
        raw = "```JSON\n{\"a\": 1}\n```"
        assert _strip_code_fences(raw) == '{"a": 1}'


class TestLenientJsonLoads:
    def test_valid_json(self) -> None:
        assert _lenient_json_loads('{"a": 1}') == {"a": 1}

    def test_trailing_comma(self) -> None:
        # json_repair handles this
        assert _lenient_json_loads('{"a": 1,}') == {"a": 1}

    def test_returns_empty_dict_on_unparseable(self) -> None:
        # Pure garbage; both stdlib and json_repair give up
        assert _lenient_json_loads("not json at all") == {}

    def test_handles_fences(self) -> None:
        assert _lenient_json_loads('```json\n{"a": 1}\n```') == {"a": 1}


class TestParseResponse:
    def test_canonical_response(self) -> None:
        raw = json.dumps({
            "intent": "factual_lookup",
            "entities": ["person:marcelo"],
            "predicates": ["email"],
            "rewrites": ["alt1", "alt2"],
            "language_hint": "en",
        })
        out = _parse_response(raw, "original")
        assert out.intent == "factual_lookup"
        assert out.entities == ("person:marcelo",)
        assert out.predicates == ("email",)
        assert "original" in out.rewrites  # anchor always present
        assert "alt1" in out.rewrites
        assert "alt2" in out.rewrites
        assert out.language_hint == "en"
        assert out.used_llm is True

    def test_original_is_first_rewrite(self) -> None:
        raw = json.dumps({"rewrites": ["alt"]})
        out = _parse_response(raw, "the original")
        assert out.rewrites[0] == "the original"

    def test_does_not_duplicate_original(self) -> None:
        raw = json.dumps({"rewrites": ["the original", "alt"]})
        out = _parse_response(raw, "the original")
        # Only one copy of the original
        assert sum(1 for r in out.rewrites if r == "the original") == 1

    def test_caps_at_five_rewrites(self) -> None:
        raw = json.dumps({
            "rewrites": ["r1", "r2", "r3", "r4", "r5", "r6", "r7"],
        })
        out = _parse_response(raw, "original")
        assert len(out.rewrites) == 5
        # First slot reserved for original
        assert out.rewrites[0] == "original"

    def test_passthrough_on_empty_response(self) -> None:
        out = _parse_response("", "original")
        assert out.rewrites == ("original",)
        assert out.used_llm is False

    def test_passthrough_on_invalid_json(self) -> None:
        out = _parse_response("not json", "original")
        assert out.rewrites == ("original",)
        assert out.used_llm is False

    def test_parses_markdown_wrapped_json(self) -> None:
        raw = '```json\n{"intent":"list","entities":["x:y"],"rewrites":["a"]}\n```'
        out = _parse_response(raw, "q")
        assert out.intent == "list"
        assert "x:y" in out.entities

    def test_handles_trailing_comma(self) -> None:
        raw = '{"intent":"factual_lookup","rewrites":["a",],}'
        out = _parse_response(raw, "q")
        # json_repair recovers; intent should be set
        assert out.intent == "factual_lookup"

    def test_drops_non_string_rewrites(self) -> None:
        raw = json.dumps({"rewrites": ["valid", 42, None, ""]})
        out = _parse_response(raw, "q")
        # Only the valid string + the original anchor
        assert "valid" in out.rewrites
        assert 42 not in out.rewrites
        assert None not in out.rewrites


# ---------------------------------------------------------------------------
# rewrite_query — sync wrapper using stub LLM
# ---------------------------------------------------------------------------


def _make_stub_llm(response: str):
    """Return a callable that records prompts and returns canned text."""
    captured: list[str] = []

    def stub(prompt: str, *, model: str) -> str:
        captured.append(prompt)
        return response

    stub.captured = captured  # type: ignore[attr-defined]
    return stub


class TestRewriteQuery:
    def setup_method(self) -> None:
        clear_cache()

    def test_returns_passthrough_for_empty_query(self) -> None:
        out = rewrite_query("", llm_invoke=_make_stub_llm(""), use_cache=False)
        assert out.rewrites == ("",)
        assert out.used_llm is False

    def test_returns_passthrough_for_whitespace_only(self) -> None:
        out = rewrite_query("   ", llm_invoke=_make_stub_llm(""), use_cache=False)
        assert out.used_llm is False

    def test_passthrough_on_llm_exception(self) -> None:
        def failing(prompt: str, *, model: str) -> str:
            raise RuntimeError("rate limit")

        out = rewrite_query("test", llm_invoke=failing, use_cache=False)
        assert out.rewrites == ("test",)
        assert out.used_llm is False

    def test_uses_cache_when_enabled(self) -> None:
        stub = _make_stub_llm(json.dumps({
            "intent": "factual_lookup",
            "rewrites": ["alt"],
        }))
        rewrite_query("query1", llm_invoke=stub, use_cache=True)
        rewrite_query("query1", llm_invoke=stub, use_cache=True)  # cache hit
        # Stub called once for query1 even on second invocation.
        assert len(stub.captured) == 1  # type: ignore[attr-defined]

    def test_different_queries_independent_cache(self) -> None:
        stub = _make_stub_llm(json.dumps({"rewrites": ["alt"]}))
        rewrite_query("q1", llm_invoke=stub, use_cache=True)
        rewrite_query("q2", llm_invoke=stub, use_cache=True)
        assert len(stub.captured) == 2  # type: ignore[attr-defined]

    def test_cache_normalizes_whitespace(self) -> None:
        stub = _make_stub_llm(json.dumps({"rewrites": ["alt"]}))
        rewrite_query("hello world", llm_invoke=stub, use_cache=True)
        # Different whitespace, same logical query.
        rewrite_query("hello   world", llm_invoke=stub, use_cache=True)
        rewrite_query("  hello world  ", llm_invoke=stub, use_cache=True)
        assert len(stub.captured) == 1  # type: ignore[attr-defined]

    def test_cache_normalizes_unicode(self) -> None:
        stub = _make_stub_llm(json.dumps({"rewrites": ["alt"]}))
        # NFC vs NFD form of "Marceló"
        rewrite_query("Marceló", llm_invoke=stub, use_cache=True)
        rewrite_query("Marceló", llm_invoke=stub, use_cache=True)
        assert len(stub.captured) == 1  # type: ignore[attr-defined]

    def test_cache_normalizes_fullwidth(self) -> None:
        stub = _make_stub_llm(json.dumps({"rewrites": ["alt"]}))
        rewrite_query("Marcelo email", llm_invoke=stub, use_cache=True)
        # Full-width Latin characters — common from CJK IME
        rewrite_query("Ｍarcelo email", llm_invoke=stub, use_cache=True)
        assert len(stub.captured) == 1  # type: ignore[attr-defined]


class TestRewriteQueryMultilingual:
    """Smoke tests for non-Latin queries — verify the pipeline doesn't
    crash on non-ASCII text. Real content quality is tested separately
    against the real LLM (manual eval)."""

    def setup_method(self) -> None:
        clear_cache()

    def test_chinese_query_passes_through(self) -> None:
        stub = _make_stub_llm(json.dumps({
            "intent": "factual_lookup",
            "entities": ["person:marcelo"],
            "predicates": ["email"],
            "rewrites": ["马塞洛邮箱", "Marcelo email"],
            "language_hint": "zh",
        }))
        out = rewrite_query("马塞洛的邮箱是什么?", llm_invoke=stub, use_cache=False)
        assert out.language_hint == "zh"
        assert "person:marcelo" in out.entities
        assert "马塞洛邮箱" in out.rewrites
        # Original query preserved as anchor
        assert "马塞洛的邮箱是什么?" in out.rewrites

    def test_japanese_query_passes_through(self) -> None:
        stub = _make_stub_llm(json.dumps({
            "intent": "factual_lookup",
            "entities": ["person:caroline"],
            "rewrites": ["キャロラインの住所", "Caroline address"],
            "language_hint": "ja",
        }))
        out = rewrite_query("キャロラインはどこに住んでいますか?", llm_invoke=stub, use_cache=False)
        assert out.language_hint == "ja"
        assert "person:caroline" in out.entities

    def test_korean_query_passes_through(self) -> None:
        stub = _make_stub_llm(json.dumps({
            "intent": "factual_lookup",
            "rewrites": ["캐롤라인 주소", "Caroline address"],
            "language_hint": "ko",
        }))
        out = rewrite_query("캐롤라인은 어디에 살아요?", llm_invoke=stub, use_cache=False)
        assert out.language_hint == "ko"

    def test_code_switching_query(self) -> None:
        stub = _make_stub_llm(json.dumps({
            "entities": ["person:marcelo"],
            "predicates": ["email"],
            "rewrites": ["Marcelo email", "马塞洛 邮箱"],
            "language_hint": "zh",
        }))
        out = rewrite_query("Marcelo的email是什么?", llm_invoke=stub, use_cache=False)
        assert "person:marcelo" in out.entities
