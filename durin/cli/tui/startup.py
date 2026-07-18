"""Startup banner for the durin TUI.

Modelled on pi-agent + hermes-agent: version + condensed keybindings,
plus a discoverability section showing the tools and skills the
current install actually has (grouped by category, hermes-style), and
a one-line profile summary at the bottom.

Goal: a fresh user can read the welcome bubble and immediately know
*what durin can do* without typing `/help`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

__all__ = [
    "build_startup_banner",
    "build_durin_logo",
    "memory_summary",
    "categorize_tools",
]


# The DURIN wordmark — figlet `banner3`, solid blocks. The TUI paints it
# with a vertical gradient in the palette's accent; see build_durin_logo().
_DURIN_WORDMARK = (
    "████████  ██     ██ ████████  ████ ██    ██\n"
    "██     ██ ██     ██ ██     ██  ██  ███   ██\n"
    "██     ██ ██     ██ ██     ██  ██  ████  ██\n"
    "██     ██ ██     ██ ████████   ██  ██ ██ ██\n"
    "██     ██ ██     ██ ██   ██    ██  ██  ████\n"
    "██     ██ ██     ██ ██    ██   ██  ██   ███\n"
    "████████   ███████  ██     ██ ████ ██    ██"
)


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    h = value.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _mix(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> str:
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def _gradient(accent: str, steps: int) -> list[str]:
    """A light→dark vertical ramp around ``accent`` — the ithildin glow."""
    rgb = _hex_to_rgb(accent)
    top = _hex_to_rgb(_mix(rgb, (255, 255, 255), 0.55))
    bottom = _hex_to_rgb(_mix(rgb, (0, 0, 0), 0.30))
    if steps <= 1:
        return [accent]
    return [_mix(top, bottom, i / (steps - 1)) for i in range(steps)]


def build_durin_logo() -> str:
    """Return the DURIN wordmark with a per-row gradient (Rich markup).

    Rendered by the TUI's ``logo`` MessageBubble role. The gradient is
    derived from the configured palette's accent, so it tracks the theme.
    """
    accent = "#57b6e6"  # Ithildin dark — the fallback
    try:
        from durin.cli.theme import detect_mode, get_palette
        from durin.config.loader import load_config

        appearance = load_config().appearance
        mode = detect_mode() if appearance.mode == "auto" else appearance.mode
        accent = get_palette(appearance.palette, mode).accent
    except Exception:  # noqa: BLE001 - the logo must never break boot
        pass
    rows = _DURIN_WORDMARK.split("\n")
    colors = _gradient(accent, len(rows))
    return "\n".join(f"[{c}]{row}[/]" for c, row in zip(colors, rows))


# ---------------------------------------------------------------------------
# Tool category map
# ---------------------------------------------------------------------------
#
# Maps tool name → category. Anything not listed falls into "misc".
# Categories are intentionally short (≤ 8 chars) so they line up nicely
# in the banner.

_TOOL_CATEGORY: dict[str, str] = {
    # filesystem
    "read_file": "file",
    "write_file": "file",
    "edit_file": "file",
    "notebook_edit": "file",
    "list_dir": "file",
    "grep": "file",
    "repo_overview": "file",
    # shell
    "exec": "shell",
    # memory
    "memory_search": "memory",
    "memory_store": "memory",
    "memory_ingest": "memory",
    "memory_drill": "memory",
    "session_search": "memory",
    # agent loop / sub-agents
    "spawn": "agent",
    "tasks": "agent",
    "subagent_monitor": "agent",
    "subagent_output": "agent",
    "long_task": "agent",
    "complete_goal": "agent",
    "todo_write": "agent",
    "enter_plan_mode": "agent",
    "exit_plan_mode": "agent",
    # web
    "web_fetch": "web",
    "web_search": "web",
    # multimodal
    "interpret_image": "media",
    "interpret_audio": "media",
    # comms
    "message": "comms",
    "ask_user_question": "comms",
    "sleep": "comms",
    # cron
    "cron": "cron",
    # introspection
    "my": "meta",
}


def categorize_tools(tool_names: list[str]) -> dict[str, list[str]]:
    """Group tools by category. Tools without a known category land in ``misc``.

    Returns categories sorted alphabetically, with tools sorted alphabetically
    inside each category.
    """
    grouped: dict[str, list[str]] = {}
    for name in tool_names:
        cat = _TOOL_CATEGORY.get(name, "misc")
        grouped.setdefault(cat, []).append(name)
    return {k: sorted(v) for k, v in sorted(grouped.items())}


def memory_summary(workspace: Path) -> dict[str, int | bool]:
    """Quantify the install: how many memory objects, vectors, sessions live here.

    Memory holds three distinct object kinds, counted separately so the banner /
    `status` can name each the way the webui does — rather than collapsing them
    into one misleading "docs" number:

    Returned keys:
    - ``memory_docs``: the raw fragment buffer — entries under the canonical
      class folders (stable / episodic / corpus / pending / session_summary).
      These are surfaced by recency and consolidated into entities by the dream.
    - ``entities``: knowledge-graph pages under ``memory/entities/<type>/`` —
      the synthesized, canonical knowledge (what the webui graph draws).
    - ``ingested_docs``: the Library shelf — ingested reference documents at
      ``memory/references/<slug>.md`` (the same set the webui Documents endpoint
      lists). One per document; ``.chunks.jsonl`` / ``.outline.json`` sidecars
      don't count.
    - ``vec_present``: True when LanceDB index files exist alongside
    - ``vec_rows``: approximate row count if we can read the index cheaply
    - ``sessions``: count of ``.jsonl`` session files
    - ``skills``: count of installed skills (custom + builtin)
    """
    out: dict[str, int | bool] = {
        "memory_docs": 0, "entities": 0, "ingested_docs": 0,
        "vec_present": False, "vec_rows": 0,
        "sessions": 0, "skills": 0,
    }
    if not workspace or not workspace.exists():
        return out

    mem = workspace / "memory"
    if mem.exists():
        # Fragment buffer: count only entries stored under the canonical class
        # folders (stable / episodic / corpus / pending / session_summary).
        # Other files under memory/ are not fragments and would inflate the
        # banner / disagree with `/memory list`.
        from durin.memory.paths import MEMORY_CLASSES, walk_class

        entry_count = 0
        for class_name in MEMORY_CLASSES:
            class_dir = mem / class_name
            if class_dir.exists():
                entry_count += sum(1 for _ in class_dir.glob("*.md"))
        out["memory_docs"] = entry_count

        # Knowledge graph: every entity page across all type folders, via the
        # canonical walker — it recurses entities/<type>/ and skips any nested
        # legacy archive/ paths, so this matches the enumeration the graph view
        # uses (top-level memory/archive/ is likewise never counted).
        out["entities"] = sum(1 for _ in walk_class(workspace, "entities"))

        # Library shelf: one row per reference document. Same glob as
        # `list_reference_documents`, so `status` matches the webui Documents
        # endpoint by construction. (Legacy installs kept ingested artifacts
        # under a per-doc `ingested/<id>/` tree; the Library is `references/`.)
        references_dir = mem / "references"
        if references_dir.is_dir():
            out["ingested_docs"] = sum(1 for _ in references_dir.glob("*.md"))

        lance_dir = mem / ".lance"
        if lance_dir.exists() and any(lance_dir.iterdir()):
            out["vec_present"] = True

    sessions = workspace / "sessions"
    if sessions.exists():
        out["sessions"] = sum(1 for _ in sessions.glob("*.jsonl"))

    skills = workspace / "skills"
    if skills.exists():
        out["skills"] = sum(1 for p in skills.iterdir() if p.is_dir())

    return out


def _list_skills(agent_loop: Any | None, workspace: Path) -> list[str]:
    """List all available skill names (built-in + workspace-local)."""
    names: set[str] = set()
    # Built-in skills live next to the durin package source.
    try:
        import durin

        builtin = Path(durin.__file__).resolve().parent / "skills"
        if builtin.exists():
            for p in builtin.iterdir():
                if p.is_dir() and (p / "SKILL.md").exists():
                    names.add(p.name)
    except Exception:  # noqa: BLE001
        pass
    # Workspace-local skills override / extend built-ins.
    ws_skills = workspace / "skills"
    if ws_skills.exists():
        for p in ws_skills.iterdir():
            if p.is_dir() and (p / "SKILL.md").exists():
                names.add(p.name)
    return sorted(names)


def _truncate_list(items: list[str], *, max_chars: int = 70) -> str:
    """Join items with ``, `` and add ``…`` if the line gets too long."""
    if not items:
        return "(none)"
    out = ""
    rendered = 0
    for i, item in enumerate(items):
        chunk = item if i == 0 else f", {item}"
        if len(out) + len(chunk) > max_chars:
            remaining = len(items) - i
            return f"{out}, … (+{remaining})" if i > 0 else f"{item}, … (+{len(items) - 1})"
        out += chunk
        rendered += 1
    return out


def build_startup_banner(*, version: str, agent_loop: Any | None) -> str:
    """Return the body for the welcome bubble.

    The bubble's role is ``banner`` (a `MessageBubble` variant with
    dim text + extra padding) so the layout is visually distinct from
    user/assistant turns.
    """
    workspace = Path(getattr(agent_loop, "workspace", "")) if agent_loop else Path()
    model = getattr(agent_loop, "model", "?") if agent_loop else "?"
    preset = getattr(agent_loop, "model_preset", None) or "default" if agent_loop else "default"

    stats = memory_summary(workspace)
    vec_marker = "✓" if stats["vec_present"] else "×"

    # Workspace path with $HOME → ~ shortening.
    ws_line = str(workspace) if workspace else "?"
    try:
        home = str(Path.home())
        if ws_line.startswith(home):
            ws_line = "~" + ws_line[len(home):]
    except Exception:  # noqa: BLE001
        pass

    # Tool + skill discovery (hermes-style).
    tool_names: list[str] = []
    try:
        tool_names = list(getattr(agent_loop, "tool_names", []) or [])
    except Exception:  # noqa: BLE001
        tool_names = []
    tools_by_cat = categorize_tools(tool_names) if tool_names else {}
    skills = _list_skills(agent_loop, workspace)

    keys = (
        "esc cancel · ctrl+q quit · / commands · ! shell · "
        "ctrl+l model · ctrl+y copy reply · ctrl+t light/dark · /theme palette"
    )

    lines: list[str] = []
    # The durin logo bubble (rendered separately) carries the name; this
    # line just states the version, model and workspace.
    lines.append(f"v{version}    ·    {model} ({preset})    ·    {ws_line}")
    lines.append(keys)
    lines.append("")

    if tools_by_cat:
        lines.append("Available tools")
        # 8-char left padding for the category name, then the list.
        for cat, members in tools_by_cat.items():
            lines.append(f"  {cat:<8}  {_truncate_list(members)}")
        lines.append("")

    if skills:
        lines.append("Available skills")
        # Show all skills on a single (possibly wrapped) line.
        lines.append(f"  {_truncate_list(skills, max_chars=120)}")
        lines.append("")

    # Profile summary line — hermes shows e.g. "30 tools · 70 skills".
    n_tools = len(tool_names)
    n_skills = len(skills)
    lines.append(
        f"Profile: {preset}  ·  {n_tools} tools  ·  {n_skills} skills"
        f"  ·  memory {stats['entities']} entities · {stats['ingested_docs']} docs"
        f" · vec{vec_marker}"
        f"  ·  /help for commands"
    )
    lines.append("")
    lines.append("Type a message, drag a file, or `/` for commands.")

    return "\n".join(lines)
