"""Rich terminal rendering for agent UI events (posture, deliberation)."""

from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


AXIS_LABELS = {
    "caution": "Caution",
    "exploration": "Exploration",
    "depth": "Depth",
    "discipline": "Discipline",
    "conformity": "Conformity",
}

ROLE_LABELS = {
    "pragmatic": "Pragmatic",
    "explorer": "Explorer",
    "critic": "Critic",
}

ROLE_COLORS = {
    "pragmatic": "blue",
    "explorer": "yellow",
    "critic": "red",
}


def _bar(value: float, width: int = 20) -> str:
    filled = int(value * width)
    return "█" * filled + "░" * (width - filled)


def render_posture_update(console: Console, data: dict[str, Any]) -> None:
    axes = data.get("axes", {})
    deltas = data.get("deltas", {})

    if not axes:
        return

    table = Table(
        show_header=False, box=None, padding=(0, 1),
        expand=False, show_edge=False,
    )
    table.add_column(width=12)
    table.add_column(width=22)
    table.add_column(width=5, justify="right")
    table.add_column(width=8)

    axis_order = ["caution", "exploration", "depth", "discipline", "conformity"]
    for axis in axis_order:
        val = axes.get(axis)
        if val is None:
            continue
        label = AXIS_LABELS.get(axis, axis)
        bar = _bar(val)
        pct = f"{val * 100:.0f}%"
        delta = deltas.get(axis, 0)
        if abs(delta) > 0.001:
            sign = "+" if delta > 0 else ""
            color = "green" if delta > 0 else "red"
            delta_str = f"[{color}]{sign}{delta * 100:.1f}%[/{color}]"
        else:
            delta_str = ""
        table.add_row(f"[dim]{label}[/dim]", f"[cyan]{bar}[/cyan]", pct, delta_str)

    panel = Panel(table, title="[dim]Posture[/dim]", border_style="dim", expand=False)
    console.print(panel)


def render_deliberation_result(console: Console, data: dict[str, Any]) -> None:
    perspectives = data.get("perspectives", {})
    synthesis = data.get("synthesis", "")
    duration_ms = data.get("duration_ms", 0)

    if not perspectives:
        return

    lines = Text()
    for role, content in perspectives.items():
        label = ROLE_LABELS.get(role, role.capitalize())
        color = ROLE_COLORS.get(role, "white")
        lines.append("● ", style=color)
        lines.append(f"{label}: ", style=f"bold {color}")
        lines.append(content[:150], style="dim")
        lines.append("\n")

    if synthesis:
        lines.append("\n")
        lines.append("Synthesis: ", style="bold")
        lines.append(synthesis[:200], style="dim italic")

    title = f"[dim]Deliberation ({duration_ms:.0f}ms)[/dim]"
    panel = Panel(lines, title=title, border_style="dim", expand=False)
    console.print(panel)


def render_agent_ui(console: Console, agent_ui: dict[str, Any]) -> bool:
    """Render a structured agent_ui blob. Returns True if handled."""
    kind = agent_ui.get("kind")
    data = agent_ui.get("data")
    if not kind or not data:
        return False

    if kind == "posture_update":
        render_posture_update(console, data)
        return True
    if kind == "deliberation_result":
        render_deliberation_result(console, data)
        return True
    return False
