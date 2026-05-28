"""Cross-encoder model validation via dynamic test (audit B12).

Per the user's feedback on the original B12 plan ("los modelos
antes de seleccionarlos y asignarlos deberian pasar un test"):
durin does NOT keep a closed enum of supported cross-encoder
models. Any model id that `sentence_transformers.CrossEncoder`
(or a custom loader) can resolve is accepted. Validation happens
dynamically when an operator clicks "Test" in the webui or runs
a CLI doctor check — not via a hardcoded list.

These tests exercise the BEHAVIOUR per
[[feedback-sync-tests-exercise-behavior]] — the helper is called
with stubbed loaders that simulate the real outcomes (success,
import error, model not found, score returns garbage).
"""

from __future__ import annotations

from typing import Any

import pytest

# Aliased import: the function name `test_model` would otherwise be
# picked up by pytest's name-based collection and run as a test.
from durin.memory.cross_encoder import test_model as probe_model


_SENTINEL = object()


class _StubScorer:
    def __init__(self, returns: Any = _SENTINEL,
                 raises: Exception | None = None) -> None:
        # Use a sentinel so `returns=[]` is treated as "score returns
        # an empty list", not "use the default [0.42]".
        self._returns = [0.42] if returns is _SENTINEL else returns
        self._raises = raises
        self.calls: list[list[tuple[str, str]]] = []

    def score(self, pairs):
        self.calls.append(pairs)
        if self._raises is not None:
            raise self._raises
        return self._returns


def test_empty_model_id_fails() -> None:
    """Empty input fails immediately without invoking any loader."""
    result = probe_model("")
    assert result["status"] == "fail"
    assert "empty" in result["message"]
    assert result["model_id"] == ""


def test_non_string_model_id_fails() -> None:
    """Defensive against bad call sites."""
    result = probe_model(None)  # type: ignore[arg-type]
    assert result["status"] == "fail"


def test_loader_returns_none_yields_helpful_message() -> None:
    """`_load_default_scorer` returns None when sentence_transformers
    is missing OR when the model id fails to load. The helper
    surfaces the install hint either way."""
    result = probe_model(
        "any/model-id",
        loader=lambda _id: None,
    )
    assert result["status"] == "fail"
    assert "sentence_transformers" in result["message"]
    assert result["model_id"] == "any/model-id"


def test_loader_raises_is_caught_with_typed_message() -> None:
    """A loader that raises (e.g. network error) becomes a `fail`
    status with the exception type + message."""

    def boom(_id: str) -> Any:
        raise RuntimeError("simulated network failure")

    result = probe_model("any/model-id", loader=boom)
    assert result["status"] == "fail"
    assert "RuntimeError" in result["message"]
    assert "simulated network failure" in result["message"]


def test_happy_path_returns_score_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """Loader succeeds + scorer scores → status ok, message includes
    the actual score for operator confidence."""
    scorer = _StubScorer(returns=[0.789])
    result = probe_model("custom/model", loader=lambda _id: scorer)
    assert result["status"] == "ok"
    assert "custom/model" in result["message"]
    assert "0.789" in result["message"]
    assert result["model_id"] == "custom/model"
    assert result["duration_ms"] >= 0.0
    # The helper used the right test pair shape.
    assert scorer.calls == [[("test query", "test document")]]


def test_score_raises_is_caught() -> None:
    """Loader succeeds but `score()` raises → fail with explanation."""
    scorer = _StubScorer(raises=ValueError("simulated GPU OOM"))
    result = probe_model("custom/model", loader=lambda _id: scorer)
    assert result["status"] == "fail"
    assert "loaded but score() raised" in result["message"]
    assert "ValueError" in result["message"]


def test_score_returns_garbage_is_caught() -> None:
    """Loader succeeds but `score()` returns an unusable shape (e.g.
    None, empty list) → fail with explanation."""
    scorer = _StubScorer(returns=[])
    result = probe_model("custom/model", loader=lambda _id: scorer)
    assert result["status"] == "fail"
    assert "unusable result" in result["message"]


def test_no_hardcoded_model_enum_in_config_schema() -> None:
    """B12 invariant: the cross-encoder config does NOT carry a
    hardcoded enum of valid model ids. Any id that the dynamic
    test accepts is valid."""
    from durin.config.schema import CrossEncoderConfig

    field = CrossEncoderConfig.model_fields["model"]
    # The metadata of a constrained field would have `examples`,
    # `pattern`, or be a Literal. A free-form `str` field has none
    # of these.
    assert field.annotation is str, (
        f"CrossEncoderConfig.model annotation should be `str` "
        f"(free-form) so any model id can be tested live; got "
        f"{field.annotation!r}. Audit B12 deliberately keeps this "
        f"open."
    )
