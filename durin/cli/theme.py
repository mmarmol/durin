"""durin design tokens — the Python mirror of ``design/tokens.css``.

The single source of truth for colour is ``design/tokens.css``. The TUI
(Textual) and the install wizard can't read CSS, so this module restates
the same hex values. ``tests/cli/test_theme_tokens.py`` pins the two
together — if they drift, it fails.

Two axes: palette (``ithildin`` default · ``forge`` · ``mithril``) and
mode (``light`` · ``dark``). See ``design/DESIGN.md``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

__all__ = [
    "Palette",
    "PALETTES",
    "PALETTE_NAMES",
    "DEFAULT_PALETTE",
    "get_palette",
    "detect_mode",
    "textual_theme",
]


@dataclass(frozen=True, slots=True)
class Palette:
    """One palette in one mode — the 13 solid colour roles of a durin theme.

    Mirrors the ``--*`` custom properties in ``design/tokens.css`` (the
    translucent ``--accent-soft`` is web-only and omitted here).
    """

    name: str
    mode: str  # "light" | "dark"
    bg: str
    surface: str
    surface_2: str
    text: str
    muted: str
    border: str
    border_strong: str
    accent: str
    accent_text: str
    accent_ink: str
    ok: str
    warn: str
    danger: str


# Values transcribed from design/tokens.css — keep them identical.
PALETTES: dict[str, dict[str, Palette]] = {
    "ithildin": {
        "light": Palette(
            name="ithildin", mode="light",
            bg="#ffffff", surface="#ffffff", surface_2="#f4f5f6",
            text="#16181a", muted="#6b7075",
            border="#e5e6e8", border_strong="#d4d6d8",
            accent="#2b9fd4", accent_text="#ffffff", accent_ink="#1b7aa8",
            ok="#1f9d57", warn="#b07d1e", danger="#c8453b",
        ),
        "dark": Palette(
            name="ithildin", mode="dark",
            bg="#0e1011", surface="#17191b", surface_2="#212325",
            text="#e7e9ec", muted="#888d93",
            border="#282b2e", border_strong="#34383c",
            accent="#57b6e6", accent_text="#06222f", accent_ink="#9bd4f1",
            ok="#5ec88a", warn="#d9a441", danger="#e5736b",
        ),
    },
    "forge": {
        "light": Palette(
            name="forge", mode="light",
            bg="#faf8f4", surface="#ffffff", surface_2="#f3efe9",
            text="#1a1612", muted="#6f6557",
            border="#e8e3da", border_strong="#d8d1c4",
            accent="#c26a2a", accent_text="#ffffff", accent_ink="#9a5420",
            ok="#6f8a2e", warn="#b07d1e", danger="#c8453b",
        ),
        "dark": Palette(
            name="forge", mode="dark",
            bg="#14110e", surface="#1d1916", surface_2="#272118",
            text="#f0ebe3", muted="#95897b",
            border="#2e2924", border_strong="#3b352e",
            accent="#e0843f", accent_text="#1c1006", accent_ink="#efab74",
            ok="#9fbd5a", warn="#d9a441", danger="#e5736b",
        ),
    },
    "mithril": {
        "light": Palette(
            name="mithril", mode="light",
            bg="#fbfbfc", surface="#ffffff", surface_2="#f1f2f3",
            text="#18191b", muted="#6c6f74",
            border="#e6e7e9", border_strong="#d5d6d8",
            accent="#3f4247", accent_text="#ffffff", accent_ink="#3f4247",
            ok="#5b5e63", warn="#b07d1e", danger="#c8453b",
        ),
        "dark": Palette(
            name="mithril", mode="dark",
            bg="#101113", surface="#191a1c", surface_2="#232527",
            text="#e9eaec", muted="#8a8d92",
            border="#2a2b2e", border_strong="#36373a",
            accent="#cfd3d8", accent_text="#16181a", accent_ink="#eef0f2",
            ok="#b9bdc2", warn="#d9a441", danger="#e5736b",
        ),
    },
}

PALETTE_NAMES: tuple[str, ...] = ("ithildin", "forge", "mithril")
DEFAULT_PALETTE = "ithildin"


def get_palette(name: str = DEFAULT_PALETTE, mode: str = "dark") -> Palette:
    """Return one palette, falling back to the default on an unknown name."""
    by_mode = PALETTES.get(name) or PALETTES[DEFAULT_PALETTE]
    return by_mode.get(mode) or by_mode["dark"]


def detect_mode(default: str = "dark") -> str:
    """Guess the terminal's light/dark from ``COLORFGBG``.

    The xterm ``COLORFGBG`` convention reports ``fg;bg`` ANSI indices; a
    background of 7 or 15 means a light terminal. Anything ambiguous
    returns ``default`` (durin is dark-native).
    """
    raw = os.environ.get("COLORFGBG", "").strip()
    if not raw:
        return default
    parts = raw.split(";")
    try:
        bg = int(parts[-1])
    except (ValueError, IndexError):
        return default
    return "light" if bg in (7, 15) else "dark"


def textual_theme(palette: str = DEFAULT_PALETTE, mode: str = "dark"):
    """Build the Textual ``Theme`` for a durin palette/mode.

    Imported lazily so this module stays usable without Textual (the
    install wizard only needs the raw :class:`Palette`).
    """
    from textual.theme import Theme

    p = get_palette(palette, mode)
    return Theme(
        name=f"durin-{p.name}-{p.mode}",
        primary=p.accent,
        secondary=p.accent_ink,
        accent=p.accent,
        foreground=p.text,
        background=p.bg,
        surface=p.surface,
        panel=p.surface_2,
        success=p.ok,
        warning=p.warn,
        error=p.danger,
        dark=(p.mode == "dark"),
        variables={
            "border": p.border,
            "border-blurred": p.border,
            "text-muted": p.muted,
        },
    )
