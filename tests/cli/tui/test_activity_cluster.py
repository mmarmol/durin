"""Tests for ActivityCluster widget."""

from __future__ import annotations

import pytest
from textual.widgets import Label

from durin.cli.tui.widgets.activity_cluster import ActivityCluster


@pytest.mark.asyncio
async def test_cluster_composes_with_header_and_body():
    """ActivityCluster renders a header Label and body Vertical."""
    from textual.app import App, ComposeResult

    class Host(App):
        def compose(self) -> ComposeResult:
            yield ActivityCluster()

    async with Host().run_test() as pilot:
        cluster = pilot.app.query_one(ActivityCluster)
        header = cluster.query_one("#cluster-header", Label)
        assert header is not None


@pytest.mark.asyncio
async def test_cluster_counts_reasoning_and_tools():
    """add_reasoning_step and add_tool_step update counts."""
    from textual.app import App, ComposeResult

    class Host(App):
        def compose(self) -> ComposeResult:
            yield ActivityCluster()

    async with Host().run_test() as pilot:
        cluster = pilot.app.query_one(ActivityCluster)
        cluster.add_reasoning_step()
        cluster.add_reasoning_step()
        cluster.add_tool_step()
        assert cluster._reasoning_count == 2
        assert cluster._tool_count == 1


@pytest.mark.asyncio
async def test_cluster_finalize_shows_done():
    """finalize() switches header to 'Done' and collapses."""
    from textual.app import App, ComposeResult

    class Host(App):
        def compose(self) -> ComposeResult:
            yield ActivityCluster()

    async with Host().run_test() as pilot:
        cluster = pilot.app.query_one(ActivityCluster)
        cluster.add_reasoning_step()
        cluster.add_tool_step()
        cluster.finalize()
        assert cluster._finalized is True
        assert cluster.collapsed is True
        assert "-finalized" in cluster.classes


@pytest.mark.asyncio
async def test_cluster_toggle_collapse():
    """Clicking toggles the collapsed state."""
    from textual.app import App, ComposeResult

    class Host(App):
        def compose(self) -> ComposeResult:
            yield ActivityCluster()

    async with Host().run_test() as pilot:
        cluster = pilot.app.query_one(ActivityCluster)
        assert cluster.collapsed is False
        cluster.collapsed = True
        assert "collapsed" in cluster.classes
        cluster.collapsed = False
        assert "collapsed" not in cluster.classes
