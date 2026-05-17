"""Rich terminal rendering for agent UI events (posture, deliberation)."""

from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


AXIS_LABELS = {
    "cautela": "Cautela",
    "exploracion": "Exploración",
    "profundidad": "Profundidad",
    "disciplina": "Disciplina",
    "conformidad": "Conformidad",
}

ROLE_LABELS = {
    "pragmatico": "Pragmático",
    "explorador": "Explorador",
    "critico": "Crítico",
}

ROLE_COLORS = {
    "pragmatico": "blue",
    "explorador": "yellow",
    "critico": "red",
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

    axis_order = ["cautela", "exploracion", "profundidad", "disciplina", "conformidad"]
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

    panel = Panel(table, title="[dim]Postura[/dim]", border_style="dim", expand=False)
    console.print(panel)


def render_deliberation_result(console: Console, data: dict[str, Any]) -> None:
    winner = data.get("winner")
    proposals = data.get("proposals", [])
    threshold = data.get("threshold", 0)
    rounds_used = data.get("rounds_used", 1)
    under_doubt = data.get("under_doubt", False)

    if not winner:
        return

    winner_role = winner["role"]
    winner_label = ROLE_LABELS.get(winner_role, winner_role)
    winner_color = ROLE_COLORS.get(winner_role, "white")
    score_str = f"{winner['score'] * 10:.1f}/10"
    threshold_str = f"{threshold * 10:.1f}"

    header = Text()
    header.append("● ", style=winner_color)
    header.append(winner_label, style=f"bold {winner_color}")
    header.append(f"  score {score_str}  umbral {threshold_str}", style="dim")
    if under_doubt:
        header.append("  ⚠ bajo duda", style="yellow")
    header.append(f"  ({rounds_used}r)", style="dim")

    lines = Text()
    lines.append_text(header)
    lines.append("\n")

    content_preview = winner["content"][:200]
    lines.append(f"  {content_preview}", style="dim italic")

    if len(proposals) > 1:
        lines.append("\n")
        for p in proposals:
            if p["role"] == winner_role:
                continue
            role_label = ROLE_LABELS.get(p["role"], p["role"])
            role_color = ROLE_COLORS.get(p["role"], "white")
            p_score = f"{p['score'] * 10:.1f}"
            lines.append(f"\n  ", style="")
            lines.append("○ ", style=role_color)
            lines.append(f"{role_label} ", style=role_color)
            lines.append(f"{p_score}/10 ", style="dim")
            p_preview = p["content"][:120]
            lines.append(p_preview, style="dim")

    panel = Panel(lines, title="[dim]Deliberación[/dim]", border_style="dim", expand=False)
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
