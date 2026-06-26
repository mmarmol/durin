"""Tests for ModesService — lists registered agent modes for the picker."""

import pytest

from durin.service.modes import ModesListQuery, ModesService
from durin.service.principal import Principal


@pytest.mark.asyncio
async def test_list_exposes_builtins_with_flag_and_description():
    result = await ModesService().list(ModesListQuery(), Principal.local())
    by_name = {m["name"]: m for m in result.modes}
    # The three built-ins are always registered.
    assert {"build", "plan", "explore"} <= set(by_name)
    for name in ("build", "plan", "explore"):
        assert by_name[name]["builtin"] is True
        assert by_name[name]["description"]  # human-readable, non-empty
        assert "icon" in by_name[name]  # present in the DTO, may be None
    # build is full access and ships no icon → the picker falls back to a glyph.
    assert by_name["build"]["icon"] is None
