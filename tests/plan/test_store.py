"""Tests for plan persistent storage."""

import json
from pathlib import Path

from durin.plan.store import PlanStore
from durin.plan.types import ExecutionTier, Phase, PlanItem, PlanState


class TestPlanStore:
    def test_save_and_load_state(self, tmp_path: Path):
        store = PlanStore(tmp_path, "sess1")
        state = PlanState(
            goal="fix astropy",
            tier=ExecutionTier.FULL_PLAN,
            items=[PlanItem("read file", status="done", added_at_cycle=1, completed_at_cycle=1)],
            current_phase=Phase.EXECUTE,
            cycle_count=2,
        )
        store.save_state(state)
        loaded = store.load_state()
        assert loaded is not None
        assert loaded.goal == "fix astropy"
        assert loaded.tier == ExecutionTier.FULL_PLAN
        assert loaded.current_phase == Phase.EXECUTE
        assert loaded.cycle_count == 2
        assert len(loaded.items) == 1
        assert loaded.items[0].status == "done"

    def test_append_event(self, tmp_path: Path):
        store = PlanStore(tmp_path, "sess2")
        store.append_event("tier_set", tier="full_plan")
        store.append_event("phase_transition", from_phase="investigate", to_phase="plan")

        events_file = tmp_path / "plans" / "sess2" / "events.jsonl"
        lines = events_file.read_text().strip().split("\n")
        assert len(lines) == 2
        e1 = json.loads(lines[0])
        assert e1["type"] == "tier_set"
        assert e1["tier"] == "full_plan"
        assert "ts" in e1

    def test_load_state_nonexistent(self, tmp_path: Path):
        store = PlanStore(tmp_path, "nonexistent")
        assert store.load_state() is None

    def test_creates_directory(self, tmp_path: Path):
        store = PlanStore(tmp_path, "new_session")
        assert (tmp_path / "plans" / "new_session").is_dir()
