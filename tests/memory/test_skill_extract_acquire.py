"""Path B (autonomous skill acquisition) must be wired into the skill-extract pass.

Regression guard: the entity-centric migration removed the previous host of
`skill_acquire_seed`. The replacement — the daily `memory_dream` skill-extract
pass (`run_skill_extract_pass`) — must now host Path B, or the dream can only
ever author skills from scratch and the autonomous-acquire capability stays orphaned.
"""
from durin.agent.tools.file_state import FileStates
from durin.memory.dream_passes import _build_skill_extract_tools


def test_skill_extract_toolset_wires_acquire_on_gap(tmp_path):
    tools = _build_skill_extract_tools(tmp_path, FileStates())
    for name in ("read_file", "edit_file", "skill_write",
                 "skill_search", "skill_acquire_seed"):
        assert tools.get(name) is not None, f"{name} missing from skill-extract toolset"


def test_skill_extract_acquire_seed_carries_allowlist(tmp_path):
    """The seed tool must be constructed with the config allowlist — its in-code
    gate is what keeps the AUTONOMOUS dream safe (empty allowlist → nothing seeds,
    author from scratch). A tool built without it would defeat the safety floor."""
    tools = _build_skill_extract_tools(tmp_path, FileStates())
    seed = tools.get("skill_acquire_seed")
    assert hasattr(seed, "_allowlist")
