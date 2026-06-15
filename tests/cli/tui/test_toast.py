"""Tests for toast notification widget."""

from __future__ import annotations

import pytest

from durin.cli.tui.widgets.toast import ToastNotification


@pytest.mark.asyncio
async def test_toast_mounts_and_displays():
    from textual.app import App, ComposeResult

    class Host(App):
        def compose(self) -> ComposeResult:
            yield ToastNotification("Copied!", level="success")

    async with Host().run_test() as pilot:
        toast = pilot.app.query_one(ToastNotification)
        assert toast is not None


@pytest.mark.asyncio
async def test_toast_auto_dismisses():
    from textual.app import App, ComposeResult

    class Host(App):
        def compose(self) -> ComposeResult:
            yield ToastNotification("Test", level="info", duration=0.05)

    async with Host().run_test() as pilot:
        toast = pilot.app.query_one(ToastNotification)
        assert toast is not None
        # Wait for auto-dismiss
        import asyncio

        await asyncio.sleep(0.15)
        await pilot.pause()
        # Toast should be removed
        toasts = list(pilot.app.query(ToastNotification))
        assert len(toasts) == 0


@pytest.mark.asyncio
async def test_toast_has_level_class():
    from textual.app import App, ComposeResult

    class Host(App):
        def compose(self) -> ComposeResult:
            yield ToastNotification("Warning!", level="warning")

    async with Host().run_test() as pilot:
        toast = pilot.app.query_one(ToastNotification)
        assert "-warning" in toast.classes
