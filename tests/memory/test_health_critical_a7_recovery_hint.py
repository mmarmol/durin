"""`memory.health.critical` carries a `manual_recovery_hint`.

- The payload includes a `manual_recovery_hint` (string) — the CLI
  command an operator runs to rebuild the failed component.
- The hint per component MUST reference a real CLI subcommand and
  a real `--target` value, or the hint goes stale silently the
  next time someone renames a target.

Anti-drift tests exercise the real CLI validation (without executing the
command), not just compare strings. If `durin memory reindex` renames or
its `--target` accepted set changes, this test fails loudly.
"""

from __future__ import annotations

import re

import pytest

from durin.cli.memory_cmd import VALID_REINDEX_TARGETS
from durin.memory.health_check import (
    _RECOVERY_HINT_FALLBACK,
    _RECOVERY_HINTS,
    _emit_critical,
)

_TARGET_RE = re.compile(r"--target\s+(\S+)")


def _extract_target(hint: str) -> str | None:
    """Pull the `--target X` value out of a recovery-hint string."""
    match = _TARGET_RE.search(hint)
    return match.group(1) if match else None


def test_all_known_probes_have_a_recovery_hint() -> None:
    """Every component the HealthChecker probes must have a hint.
    Currently `fts` and `lance` are probed (see
    `durin/memory/health_check.py::HealthChecker.run_tick`)."""
    for probe in ("fts", "lance"):
        assert probe in _RECOVERY_HINTS, (
            f"missing recovery hint for probe {probe!r}; add it to "
            f"_RECOVERY_HINTS in durin/memory/health_check.py"
        )


def test_hints_use_the_canonical_durin_memory_reindex_prefix() -> None:
    """The real command is `durin memory reindex` (not `durin reindex`).
    This guards against re-introducing that typo."""
    for component, hint in _RECOVERY_HINTS.items():
        assert hint.startswith("durin memory reindex"), (
            f"hint for {component!r} does not start with the canonical "
            f"`durin memory reindex` prefix: {hint!r}"
        )
    assert _RECOVERY_HINT_FALLBACK.startswith("durin memory reindex"), (
        f"fallback hint must use the canonical prefix: "
        f"{_RECOVERY_HINT_FALLBACK!r}"
    )


def test_hint_targets_pass_cli_validation() -> None:
    """Anti-drift: every `--target X` in every hint must be a value
    the CLI actually accepts. If someone renames a `cmd_reindex`
    target without updating `_RECOVERY_HINTS`, this test fails."""
    for component, hint in _RECOVERY_HINTS.items():
        target = _extract_target(hint)
        assert target is not None, (
            f"could not extract --target value from hint for {component!r}: "
            f"{hint!r}"
        )
        assert target in VALID_REINDEX_TARGETS, (
            f"hint for {component!r} suggests `--target {target}`, "
            f"but the CLI rejects it. VALID_REINDEX_TARGETS = "
            f"{VALID_REINDEX_TARGETS}. Update _RECOVERY_HINTS or the "
            f"CLI to agree."
        )
    fallback_target = _extract_target(_RECOVERY_HINT_FALLBACK)
    assert fallback_target in VALID_REINDEX_TARGETS, (
        f"fallback hint target {fallback_target!r} not in "
        f"VALID_REINDEX_TARGETS"
    )


def test_emit_critical_includes_hint_for_known_component(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the emit path: when `_emit_critical("lance", ...)`
    fires, the payload carries the hint for `lance` — not the
    fallback."""
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "durin.memory.health_check.emit_tool_event",
        lambda t, d: events.append((t, d)),
    )
    _emit_critical(
        "lance", error="lance probe: connection refused", count=3,
    )
    assert len(events) == 1
    name, payload = events[0]
    assert name == "memory.health.critical"
    assert payload["component"] == "lance"
    assert payload["consecutive_failures"] == 3
    assert payload["manual_recovery_hint"] == _RECOVERY_HINTS["lance"]


def test_emit_critical_uses_fallback_for_unknown_component(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a new probe is added but its recovery hint isn't, the emit
    path uses `_RECOVERY_HINT_FALLBACK` rather than crashing."""
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "durin.memory.health_check.emit_tool_event",
        lambda t, d: events.append((t, d)),
    )
    _emit_critical(
        "watcher", error="watcher probe: file descriptor exhausted",
        count=3,
    )
    payload = events[0][1]
    assert payload["manual_recovery_hint"] == _RECOVERY_HINT_FALLBACK


def test_typed_dict_declares_hint() -> None:
    """The TypedDict declares `manual_recovery_hint`. Catches a silent
    schema revert."""
    from durin.telemetry.schema import MemoryHealthCriticalEvent

    annotations = MemoryHealthCriticalEvent.__annotations__
    assert "manual_recovery_hint" in annotations
    # Pre-A7 fields still required.
    for required in ("component", "consecutive_failures", "last_error"):
        assert required in annotations, (
            f"pre-A7 field {required!r} missing — A7 was additive"
        )
