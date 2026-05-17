"""Tests for plan system tools."""

import pytest

from durin.plan.hook import PlanHook
from durin.agent.tools.plan import SetExecutionModeTool, UpdatePlanTool, set_plan_hook
from durin.plan.types import ExecutionTier, Phase


class TestSetExecutionModeTool:
    @pytest.mark.asyncio
    async def test_sets_tier_via_hook(self):
        hook = PlanHook()
        set_plan_hook(hook)
        tool = SetExecutionModeTool()
        result = await tool.execute(tier="plan", reason="complex task")
        assert "plan" in result
        assert hook.state.tier == ExecutionTier.PLAN

    @pytest.mark.asyncio
    async def test_sets_direct(self):
        hook = PlanHook()
        set_plan_hook(hook)
        tool = SetExecutionModeTool()
        result = await tool.execute(tier="direct")
        assert "direct" in result

    def test_schema(self):
        tool = SetExecutionModeTool()
        schema = tool.parameters
        assert "tier" in schema["properties"]
        assert schema["properties"]["tier"]["enum"] == ["direct", "plan"]


class TestUpdatePlanTool:
    @pytest.mark.asyncio
    async def test_add_item(self):
        hook = PlanHook()
        hook.set_tier(ExecutionTier.PLAN)
        set_plan_hook(hook)
        tool = UpdatePlanTool()
        result = await tool.execute(action="add", item="Read the file")
        assert "Added" in result
        assert len(hook.state.items) == 1

    @pytest.mark.asyncio
    async def test_requires_plan_mode(self):
        hook = PlanHook()
        hook.set_tier(ExecutionTier.DIRECT)
        set_plan_hook(hook)
        tool = UpdatePlanTool()
        result = await tool.execute(action="add", item="something")
        assert "only available in plan mode" in result

    @pytest.mark.asyncio
    async def test_complete_item(self):
        hook = PlanHook()
        hook.set_tier(ExecutionTier.PLAN)
        set_plan_hook(hook)
        tool = UpdatePlanTool()
        await tool.execute(action="add", item="Fix bug")
        result = await tool.execute(action="complete", item="Fix bug")
        assert "Completed" in result

    @pytest.mark.asyncio
    async def test_no_hook(self):
        set_plan_hook(None)
        tool = UpdatePlanTool()
        result = await tool.execute(action="add", item="x")
        assert "not active" in result
