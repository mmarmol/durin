"""Quarantine logic for repeatedly failing entities.

Per `docs/memory/05_dream_cold_path.md` §12.5:

- Structural failures (`VALIDATION`, `PATCH_RUNTIME`, `ROUND_TRIP`)
  increment ``dream_failure_count`` on the entity page's frontmatter.
- Ambient failures (LLM call, IO) do NOT count — they don't indicate
  a problem with the entity itself.
- 3 consecutive structural failures → set
  ``dream_quarantine: now + 7d``.
- The runner skips entities with a future ``dream_quarantine``.
- A successful apply resets both fields.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from durin.memory.dream_apply import DreamApplyFailureKind
from durin.memory.dream_quarantine import (
    QUARANTINE_DURATION,
    STRUCTURAL_FAILURE_KINDS,
    clear_failures,
    is_quarantined,
    record_failure,
)
from durin.memory.entity_page import EntityPage


def _fresh_page() -> EntityPage:
    return EntityPage(type="person", name="Marcelo", aliases=[])


def test_quarantine_duration_is_seven_days() -> None:
    assert QUARANTINE_DURATION == timedelta(days=7)


def test_structural_kinds_are_validation_patch_round_trip() -> None:
    assert STRUCTURAL_FAILURE_KINDS == {
        DreamApplyFailureKind.VALIDATION,
        DreamApplyFailureKind.PATCH_RUNTIME,
        DreamApplyFailureKind.ROUND_TRIP,
    }


# ---------------------------------------------------------------------------
# record_failure
# ---------------------------------------------------------------------------


def test_first_structural_failure_increments_counter() -> None:
    page = _fresh_page()
    record_failure(page, DreamApplyFailureKind.VALIDATION)
    assert page.extra["dream_failure_count"] == 1
    assert "dream_quarantine" not in page.extra


def test_second_structural_failure_increments_to_two() -> None:
    page = _fresh_page()
    record_failure(page, DreamApplyFailureKind.PATCH_RUNTIME)
    record_failure(page, DreamApplyFailureKind.ROUND_TRIP)
    assert page.extra["dream_failure_count"] == 2
    assert "dream_quarantine" not in page.extra


def test_third_structural_failure_quarantines() -> None:
    page = _fresh_page()
    for _ in range(3):
        record_failure(page, DreamApplyFailureKind.VALIDATION)
    assert page.extra["dream_failure_count"] == 3
    quarantine = page.extra["dream_quarantine"]
    assert isinstance(quarantine, str)
    # Within a few seconds of now+7d.
    parsed = datetime.fromisoformat(quarantine.replace("Z", "+00:00"))
    delta = parsed - datetime.now(timezone.utc)
    assert timedelta(days=6, hours=23) < delta < timedelta(days=7, minutes=5)


def test_ambient_failures_do_not_increment() -> None:
    page = _fresh_page()
    record_failure(page, DreamApplyFailureKind.IO)
    record_failure(page, DreamApplyFailureKind.IO)
    record_failure(page, DreamApplyFailureKind.IO)
    assert page.extra.get("dream_failure_count", 0) == 0
    assert "dream_quarantine" not in page.extra


def test_mixed_failures_only_structural_counts() -> None:
    page = _fresh_page()
    record_failure(page, DreamApplyFailureKind.VALIDATION)  # +1
    record_failure(page, DreamApplyFailureKind.IO)          # +0
    record_failure(page, DreamApplyFailureKind.PATCH_RUNTIME)  # +1
    record_failure(page, DreamApplyFailureKind.IO)          # +0
    record_failure(page, DreamApplyFailureKind.ROUND_TRIP)  # +1 → quarantine
    assert page.extra["dream_failure_count"] == 3
    assert "dream_quarantine" in page.extra


# ---------------------------------------------------------------------------
# clear_failures
# ---------------------------------------------------------------------------


def test_clear_resets_counter_and_quarantine() -> None:
    page = _fresh_page()
    for _ in range(3):
        record_failure(page, DreamApplyFailureKind.VALIDATION)
    clear_failures(page)
    assert "dream_failure_count" not in page.extra
    assert "dream_quarantine" not in page.extra


def test_clear_on_fresh_page_is_noop() -> None:
    page = _fresh_page()
    clear_failures(page)
    assert "dream_failure_count" not in page.extra
    assert "dream_quarantine" not in page.extra


# ---------------------------------------------------------------------------
# is_quarantined
# ---------------------------------------------------------------------------


def test_fresh_page_is_not_quarantined() -> None:
    assert is_quarantined(_fresh_page()) is False


def test_future_quarantine_returns_true() -> None:
    page = _fresh_page()
    future = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    page.extra["dream_quarantine"] = future
    assert is_quarantined(page) is True


def test_past_quarantine_returns_false() -> None:
    """Expired quarantine windows do not block the entity any more."""
    page = _fresh_page()
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    page.extra["dream_quarantine"] = past
    assert is_quarantined(page) is False


def test_malformed_quarantine_string_returns_false() -> None:
    page = _fresh_page()
    page.extra["dream_quarantine"] = "not a timestamp"
    # Defensive: don't crash, return False so the entity remains
    # processable. The bad string will be overwritten on next failure
    # or cleared on next success.
    assert is_quarantined(page) is False


def test_is_quarantined_honors_explicit_now() -> None:
    page = _fresh_page()
    page.extra["dream_quarantine"] = "2026-06-15T00:00:00+00:00"
    before = datetime(2026, 6, 14, tzinfo=timezone.utc)
    after = datetime(2026, 6, 16, tzinfo=timezone.utc)
    assert is_quarantined(page, now=before) is True
    assert is_quarantined(page, now=after) is False
