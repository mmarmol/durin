# durin — Design System

> durin's visual identity across its three surfaces: the web dashboard, the
> Textual TUI, and the install wizard.
>
> **Two axes:** palette (`ithildin` · `forge` · `mithril`) × mode (`light` ·
> `dark`). **Default:** Ithildin. Tokens: [`tokens.css`](./tokens.css).

## 1. Visual Theme & Atmosphere

durin is a personal AI agent, terminal-first. The design is calm, precise and
unhurried — engineered, not decorated. Dark is the native medium (the agent
lives in a terminal); light is the faithful variant, not an afterthought.

The name is Tolkien's: Durin the dwarf-king — and the Doors of Durin, whose
ithildin lines show only by moonlight, silver-blue on grey stone. The default
palette, **Ithildin**, takes exactly that: cool slate neutrals and a single
sky-cyan that glows. **Forge** (warm near-black, ember) and **Mithril**
(achromatic silver) are alternates — the same restraint in a warm and an
achromatic register.

Across all palettes: one accent, a neutral base, generous radius, soft depth,
no ornament.

**Scope.** The system governs the three surfaces above. One-shot CLI commands
(`durin status`, `durin doctor`, `durin config`) intentionally stay at rich's
defaults — semantic colour only (✓/✗, tables), never the palette. They are
tools, not spaces; theming them is deliberately out of scope, not an oversight.

## 2. Color Palette & Roles

[`tokens.css`](./tokens.css) is the single source of truth. Three palettes,
each with a hand-tuned **light** and **dark** set — same role names, different
values. Dark is never a computed inversion of light.

| Token | Role |
|---|---|
| `--bg` | page background — the deepest surface |
| `--surface` | cards, panels, bars |
| `--surface-2` | elevated surface — message bubbles, tool blocks, inputs |
| `--text` | primary text |
| `--muted` | secondary text, metadata, placeholders |
| `--border` / `--border-strong` | hairline border / emphasized divider |
| `--accent` | the one brand colour — CTAs, focus, selection, status dot |
| `--accent-text` / `--accent-soft` / `--accent-ink` | text on accent / translucent accent fill / accent as text on a normal surface |
| `--ok` / `--warn` / `--danger` | status colours |

**Ithildin (default)** — light: `--bg #ffffff`, `--text #16181a`, `--muted
#6b7075`, `--border #e5e6e8`, `--accent #2b9fd4`. Dark: `--bg #0e1011`, `--text
#e7e9ec`, `--muted #888d93`, `--border #282b2e`, `--accent #57b6e6`. Forge and
Mithril use the same roles — see `tokens.css`.

## 3. Typography Rules

- **UI / body:** the platform sans — `-apple-system, system-ui, "Segoe UI",
  sans-serif`. No custom brand typeface.
- **Code & TUI:** monospace — `ui-monospace, "JetBrains Mono", Menlo,
  monospace`.
- **Scale (px):** 11 · 12 · 13 · 14 · 16 · 20 · 24 · 32.
- **Line-height:** 1.5 body, 1.2 headings. Letter-spacing −0.01em on display
  sizes ≥ 24px.

## 4. Component Stylings

- **Buttons:** radius `--radius-sm` (8px). Primary = `--accent` fill +
  `--accent-text`; secondary = 1px `--border`, transparent fill.
- **Cards / groups:** `--surface`, 1px `--border`, radius `--radius-xl` (22px)
  for settings groups / `--radius-lg` elsewhere, `--shadow-soft`.
- **Inputs:** `--bg` fill, 1px `--border`, radius `--radius-sm`; focus →
  `--accent` border.
- **Chips / badges:** `--radius-pill`; selected = `--accent-soft` fill +
  `--accent` border.
- **Tool blocks (chat):** `--surface-2`, 1px `--border`, radius `--radius-md`,
  monospace body.
- **Rows / list items:** separated by `--border`; comfortable min-height.

## 5. Layout Principles

- **Spacing:** 8px base unit; scale `--space-1` … `--space-8`.
- **Web:** centred content; settings as grouped cards (see
  `webui/src/components/settings/primitives.tsx`).
- **TUI:** full-screen — header · chat · input · footer.
- Content first, chrome second; whitespace is generous.

## 6. Depth & Elevation

| Level | Treatment | Use |
|---|---|---|
| 0 — flat | none | background, inline text |
| 1 — contained | 1px `--border` | standard cards, rows |
| 2 — raised | `--shadow-soft` / `--shadow-soft-dark` | elevated groups, popovers |

No heavy drop shadows. Depth comes from borders plus one soft shadow.

## 7. Do's and Don'ts

**Do:** keep exactly one accent per palette · keep the token set small (~13
roles) · hand-tune each dark set · let the wizard use the accent for emphasis
only (it can't own the terminal background) · detect the terminal's light/dark
via `COLORFGBG`.

**Don't:** introduce a second chromatic colour · hard-code a colour inside one
surface — edit `tokens.css` and propagate · use heavy drop shadows · let the
three surfaces drift apart.

## 8. Responsive Behavior

- **Web:** single column on phones; grouped cards stack; the settings nav
  collapses.
- **TUI:** reflows to the terminal; usable from ~80×24.
- **Wizard:** linear prompt flow, width-agnostic.

## 9. Agent Prompt Guide

When styling durin, reference **token roles, never raw hex** — "use `--accent`",
not "#57b6e6". To change a colour: edit [`tokens.css`](./tokens.css), then
propagate to `webui/src/globals.css` and `durin/cli/theme.py` (an anti-drift
test pins those together). The `durin-design` skill carries the full playbook.
