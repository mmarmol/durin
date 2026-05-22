"""Anti-drift guard: durin/cli/theme.py must mirror design/tokens.css.

The CSS file is the single source of truth for colour; theme.py restates
it for the TUI and wizard. This test parses tokens.css and asserts every
solid colour role matches — so the two cannot silently diverge (the
failure Hermes shipped on purpose; see the durin-design skill).
"""

from __future__ import annotations

import re
from pathlib import Path

from durin.cli.theme import PALETTES

TOKENS_CSS = Path(__file__).parents[2] / "design" / "tokens.css"

# CSS custom property -> Palette field. `--accent-soft` is web-only (rgba)
# and deliberately not mirrored in theme.py.
CSS_TO_FIELD = {
    "bg": "bg",
    "surface": "surface",
    "surface-2": "surface_2",
    "text": "text",
    "muted": "muted",
    "border": "border",
    "border-strong": "border_strong",
    "accent": "accent",
    "accent-text": "accent_text",
    "accent-ink": "accent_ink",
    "ok": "ok",
    "warn": "warn",
    "danger": "danger",
}


def _parse_tokens_css() -> dict[tuple[str, str], dict[str, str]]:
    """Return {(palette, mode): {css_var: value}} for every themed block."""
    text = re.sub(r"/\*.*?\*/", "", TOKENS_CSS.read_text(encoding="utf-8"), flags=re.S)
    blocks: dict[tuple[str, str], dict[str, str]] = {}
    for match in re.finditer(r"([^{}]+)\{([^{}]+)\}", text):
        selector, body = match.group(1), match.group(2)
        palette = re.search(r'\[data-palette="(\w+)"\]', selector)
        if palette is None:  # the shared :root block — no palette
            continue
        mode = "dark" if ".dark" in selector else "light"
        decls = dict(re.findall(r"--([\w-]+):\s*([^;]+);", body))
        blocks[(palette.group(1), mode)] = {k: v.strip() for k, v in decls.items()}
    return blocks


def test_tokens_css_covers_all_six_sets() -> None:
    blocks = _parse_tokens_css()
    expected = {(name, mode) for name in PALETTES for mode in ("light", "dark")}
    assert set(blocks) == expected


def test_theme_py_mirrors_tokens_css() -> None:
    blocks = _parse_tokens_css()
    mismatches: list[str] = []
    for (palette, mode), decls in blocks.items():
        entry = PALETTES[palette][mode]
        for css_var, field in CSS_TO_FIELD.items():
            css_value = decls.get(css_var)
            py_value = getattr(entry, field)
            if css_value != py_value:
                mismatches.append(
                    f"{palette}/{mode} --{css_var}: css={css_value!r} py={py_value!r}"
                )
    assert not mismatches, "theme.py drifted from tokens.css:\n" + "\n".join(mismatches)
