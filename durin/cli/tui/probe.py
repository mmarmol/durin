"""Headless inspection helpers for the durin TUI.

Textual's ``App.run_test()`` yields a headless ``Pilot``, but it offers
no plain-text view of what is painted — a test can only query the
widget tree. These helpers read the compositor's rendered strips, so
both the test suite and ``scripts/tui_smoke.py`` can assert on (and
show) the actual screen: the same cells a real terminal would display.
"""

from __future__ import annotations

from textual.app import App
from textual.pilot import Pilot

# Characters Textual's key system names rather than takes literally.
_KEY_ALIASES = {" ": "space", "\t": "tab", "\n": "enter"}


def screen_text(app: App) -> str:
    """Return the top screen rendered as plain text, one row per line.

    Reads the compositor strips of ``app.screen`` (the top of the
    screen stack), so while a modal is open this shows the modal.
    Trailing blank cells and rows are trimmed. Never raises: inspection
    must not crash the run it is inspecting.
    """
    try:
        strips = app.screen._compositor.render_strips()
    except Exception:  # noqa: BLE001 - inspection must stay non-fatal
        return ""
    return "\n".join(strip.text.rstrip() for strip in strips).rstrip("\n")


async def type_text(pilot: Pilot, text: str) -> None:
    """Type ``text`` into the focused widget, one real key event per char.

    Going through real key events (rather than setting a widget value)
    keeps autocomplete, suggesters and key bindings in the loop.
    """
    for char in text:
        await pilot.press(_KEY_ALIASES.get(char, char))


async def run_step(pilot: Pilot, step: str) -> None:
    """Apply one scripted step, then let the event loop settle.

    The verb is the text before the first ``:``. Grammar:

    * ``type:TEXT``        — type TEXT character by character
    * ``press:KEY[,KEY]``  — press one or more keys (Textual names,
      e.g. ``ctrl+l``, ``enter``, ``escape``)
    * ``pause``            — just let the event loop settle

    Raises ``ValueError`` on an unknown verb.
    """
    verb, _, arg = step.partition(":")
    verb = verb.strip()
    if verb == "type":
        await type_text(pilot, arg)
    elif verb == "press":
        keys = [k.strip() for k in arg.split(",") if k.strip()]
        if keys:
            await pilot.press(*keys)
    elif verb == "pause":
        pass
    else:
        raise ValueError(f"unknown step verb {verb!r} in {step!r}")
    await pilot.pause()
