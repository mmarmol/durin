"""Custom agent modes loaded from config into the registry."""

import pytest

from durin.agent import agent_mode
from durin.config.schema import ModeConfig


@pytest.fixture(autouse=True)
def _reset_registry():
    # Drop any custom modes a test registered, keeping the built-ins.
    yield
    agent_mode.register_config_modes({})


def test_config_mode_is_registered_and_listed():
    agent_mode.register_config_modes(
        {"reviewer": ModeConfig(description="Reads and comments", allowed=["read_file", "grep"])}
    )
    names = {m.name for m in agent_mode.list_modes()}
    assert "reviewer" in names
    m = agent_mode.get_mode("reviewer")
    assert m.builtin is False
    assert m.description == "Reads and comments"
    assert m.allowed == frozenset({"read_file", "grep"})
    assert m.is_tool_allowed("read_file") is True
    assert m.is_tool_allowed("edit_file") is False


def test_config_mode_cannot_shadow_a_builtin():
    agent_mode.register_config_modes(
        {"build": ModeConfig(description="hijacked", allowed=["read_file"])}
    )
    build = agent_mode.get_mode("build")
    assert build.builtin is True
    assert build.allowed is None  # still full access, not the config override


def test_reregister_replaces_previous_custom_modes():
    agent_mode.register_config_modes({"a": ModeConfig(description="A")})
    assert "a" in {m.name for m in agent_mode.list_modes()}
    agent_mode.register_config_modes({"b": ModeConfig(description="B")})
    names = {m.name for m in agent_mode.list_modes()}
    assert "b" in names and "a" not in names
    assert {"build", "plan", "explore"} <= names  # built-ins always remain
