"""Tests for the LoCoMo dataset loader — canonical category mapping
(audit H13, 2026-05-29).

Pre-H13 ``_CATEGORY_BY_CODE`` mapped codes 1..5 to ``single_hop /
multi_hop / temporal / open_domain / adversarial``, which is the
order of the LoCoMo paper §4.1 NARRATIVE but NOT the dataset's
own category-code-to-label mapping. The dataset (verified by
counting against mem0's public ``benchmarks/locomo/prompts.py``
``CATEGORY_NAMES`` dict and against the raw counts in
``locomo10.json``: 282/321/96/841/446) actually labels:

  1 = multi_hop    (282 questions)
  2 = temporal     (321 questions)
  3 = open_domain  (96 questions)
  4 = single_hop   (841 questions)
  5 = adversarial  (446 questions)

The bug silently re-labelled every prior bench: what we called
"single_hop is our worst category at 20%" was actually multi_hop;
what we celebrated as "adversarial 100%" was a sample of size 2
(442 adversarial questions had ``answer=None`` and were skipped at
load time — separate audit H14).

These tests pin the canonical mapping so a future edit of
``_CATEGORY_BY_CODE`` re-introducing the swap fails loudly.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DATASET_PATH = _REPO_ROOT / "scripts" / "benchmark" / "locomo_dataset.py"


def _load_dataset_module():
    import sys
    name = "scripts_benchmark_locomo_dataset_under_test"
    spec = importlib.util.spec_from_file_location(name, _DATASET_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_ds_mod = _load_dataset_module()


def test_category_by_code_uses_canonical_mapping() -> None:
    """The dataset's category codes (1..5) must map to the canonical
    LoCoMo labels per mem0's reference implementation.
    """
    expected = {
        1: "multi_hop",
        2: "temporal",
        3: "open_domain",
        4: "single_hop",
        5: "adversarial",
    }
    actual = dict(_ds_mod._CATEGORY_BY_CODE)
    assert actual == expected, (
        f"category mapping drifted; expected {expected}, got {actual}. "
        "Compare against mem0/memory-benchmarks/benchmarks/locomo/"
        "prompts.py::CATEGORY_NAMES."
    )


def test_categories_tuple_lists_all_five() -> None:
    cats = _ds_mod.CATEGORIES
    assert set(cats) == {
        "single_hop", "multi_hop", "temporal",
        "open_domain", "adversarial",
    }


def test_locomo10_counts_match_canonical_mapping() -> None:
    """End-to-end check: loading the bundled locomo10 dataset must
    produce the canonical per-category counts mem0 publishes —
    minus the 444 adversarial entries that carry ``answer=None``
    (those are the 'agent must refuse' adversarial cases; H14
    covers loading them with a special judge)."""
    data_path = Path.home() / ".cache" / "durin" / "locomo10.json"
    if not data_path.is_file():
        pytest.skip("locomo10.json not present in ~/.cache/durin/")
    qas = _ds_mod.load_dataset(data_path)
    import collections
    cats = collections.Counter(qa.category for qa in qas)
    # Counts from the canonical mapping (multi_hop=282, temporal=321,
    # open_domain=96, single_hop=841, adversarial=446 — 444 of the
    # 446 adversarial have answer=None and are skipped pre-H14).
    assert cats["multi_hop"] == 282, cats
    assert cats["temporal"] == 321, cats
    assert cats["open_domain"] == 96, cats
    assert cats["single_hop"] == 841, cats
    # adversarial: 2 with answer text + 444 with answer=None.
    # H14 (2026-05-29) accepts the answer=None entries with the
    # ``__REFUSE__`` sentinel so the judge can score them with a
    # refusal rubric instead of substring match.
    assert cats["adversarial"] == 446, cats


def test_adversarial_null_answers_load_with_refuse_sentinel() -> None:
    """H14 (2026-05-29): adversarial QAs with raw ``answer=null`` in
    the dataset land with ``answer="__REFUSE__"`` so the judge can
    distinguish them from substring-matchable answers."""
    data_path = Path.home() / ".cache" / "durin" / "locomo10.json"
    if not data_path.is_file():
        pytest.skip("locomo10.json not present")
    qas = _ds_mod.load_dataset(data_path)
    adv = [q for q in qas if q.category == "adversarial"]
    refuse = [q for q in adv if q.answer == "__REFUSE__"]
    assert len(refuse) == 444, (
        f"expected 444 refuse-sentinel adversarial QAs, got {len(refuse)}"
    )
    # Non-adversarial QAs with answer=None still skipped.
    other_refuse = [
        q for q in qas
        if q.category != "adversarial" and q.answer == "__REFUSE__"
    ]
    assert other_refuse == [], (
        "the __REFUSE__ sentinel must only apply to adversarial category"
    )
