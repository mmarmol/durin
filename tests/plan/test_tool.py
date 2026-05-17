"""Tests for plan system tools."""

import pytest

from durin.plan.hook import PlanHook
from durin.plan.tool import SetExecutionModeTool, UpdatePlanTool
from durin.plan.types import ExecutionTier, Phase


class TestSetExecutionModeTool:
    @pytest.mark.asyncio
    async def test_sets_tier_via_hook(self):
        hook = PlanHook()
        tool = SetExecutionModeTool(hook=hook)
        result = await tool.execute(tier="full_plan", reason="complex task")
        assert "full_plan" in result
        assert hook.state.tier == ExecutionTier.FULL_PLAN

    @pytest.mark.asyncio
    async def test_works_without_hook(self):
        tool = SetExecutionModeTool(hook=None)
        result = await tool.execute(tier="direct")
        assert "direct" in result

    def test_schema(self):
        tool = SetExecutionModeTool()
        schema = tool.parameters
        assert "tier" in schema["properties"]
        assert schema["properties"]["tier"]["enum"] == ["direct", "execute_verify", "full_plan"]


class TestUpdatePlanTool:
    @pytest.mark.asyncio
    async def test_add_item(self):
        hook = PlanHook()
        hook.set_tier(ExecutionTier.FULL_PLAN)
        tool = UpdatePlanTool(hook=hook)
        result = await tool.execute(action="add", item="Read the file")
        assert "Added" in result
        assert len(hook.state.items) == 1

    @pytest.mark.asyncio
    async def test_requires_full_plan_mode(self):
        hook = PlanHook()
        hook.set_tier(ExecutionTier.DIRECT)
        tool = UpdatePlanTool(hook=hook)
        result = await tool.execute(action="add", item="something")
        assert "only available in full_plan" in result

    @pytest.mark.asyncio
    async def test_complete_item(self):
        hook = PlanHook()
        hook.set_tier(ExecutionTier.FULL_PLAN)
        tool = UpdatePlanTool(hook=hook)
        await tool.execute(action="add", item="Fix bug")
        result = await tool.execute(action="complete", item="Fix bug")
        assert "Completed" in result

    @pytest.mark.asyncio
    async def test_no_hook(self):
        tool = UpdatePlanTool(hook=None)
        result = await tool.execute(action="add", item="x")
        assert "not active" in result
