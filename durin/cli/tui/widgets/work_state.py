"""WorkStore — folds workflow/subagent progress events into a renderable tree.

Push-driven model for the sidebar's WORK section. The agent emits one outbound
event per progress phase (`running`, then `end`/`error`) for each workflow run
and each sub-agent. This store keeps the latest snapshot per item keyed by
``call_id``, splits them into active vs. finished, and renders Textual console
markup. Kept free of Textual imports so it is unit-testable in isolation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rich.markup import escape

__all__ = ["WorkStore"]

_GLYPH = {"running": "○", "done": "✓", "failed": "✗", "pending": "○", "needs_input": "?"}
# Braille spinner frames — a running node cycles through these so the panel
# shows motion while work is in flight.
_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def _running_glyph(spin: int) -> str:
    return _SPINNER[spin % len(_SPINNER)]
_NODE_CLASS = {
    "running": "work-running",
    "done": "work-done",
    "failed": "work-failed",
    "pending": "work-pending",
    "needs_input": "work-needs-input",
}


@dataclass
class _Item:
    call_id: str
    kind: str  # "workflow" | "subagent"
    label: str
    status: str  # "running" | "done" | "failed" | "needs_input"
    detail: str = ""
    nodes: list[dict] = field(default_factory=list)


class WorkStore:
    """Ordered store of work items, latest snapshot per ``call_id``."""

    def __init__(self) -> None:
        self._items: dict[str, _Item] = {}
        self._order: list[str] = []

    def ingest(self, event: dict) -> None:
        name = str(event.get("name") or "")
        if name not in ("workflow_progress", "subagent_result"):
            return
        call_id = str(event.get("call_id") or "")
        if not call_id:
            return
        phase = str(event.get("phase") or "")
        if name == "workflow_progress":
            args = event.get("arguments") or {}
            if phase == "end":
                # Terminal frames carry the run status; a paused run is
                # "waiting for the user", not finished. Events without a
                # status (older emitters) keep the plain end→done mapping.
                run_status = str(event.get("status") or "")
                if run_status == "needs_input":
                    status = "needs_input"
                elif run_status in ("", "completed"):
                    status = "done"
                else:  # exhausted / aborted / cancelled / failed
                    status = "failed"
            else:
                status = "running"
            item = _Item(
                call_id=call_id,
                kind="workflow",
                label=str(args.get("workflow") or "workflow"),
                status=status,
                detail=str(event.get("detail") or ""),
                nodes=list(event.get("nodes") or []),
            )
        else:
            if phase == "error":
                status, detail = "failed", str(event.get("error") or "error")
            elif phase == "end":
                status, detail = "done", str(event.get("result") or "")
            else:
                prog = event.get("progress") or {}
                status = "running"
                detail = (
                    f"iter {prog.get('iteration')} · {prog.get('tool')}"
                    if prog else ""
                )
            item = _Item(
                call_id=call_id,
                kind="subagent",
                label=str(event.get("label") or "subagent"),
                status=status,
                detail=detail,
            )
        if call_id not in self._items:
            self._order.append(call_id)
        self._items[call_id] = item

    def active_count(self) -> int:
        return sum(1 for cid in self._order if self._items[cid].status == "running")

    def is_empty(self) -> bool:
        return not self._order

    def render_markup(self, spin: int = 0) -> str:
        """Render the WORK section. ``spin`` advances the running-node spinner."""
        if self.is_empty():
            return ""
        # A needs_input run belongs with the active items: it is waiting for
        # the user, not finished — parking it under "Finished" would hide it.
        active = [
            self._items[c] for c in self._order
            if self._items[c].status in ("running", "needs_input")
        ]
        finished = [
            self._items[c] for c in self._order
            if self._items[c].status not in ("running", "needs_input")
        ]
        running_n = sum(1 for it in active if it.status == "running")
        waiting_n = len(active) - running_n
        counts: list[str] = []
        if running_n or not waiting_n:
            counts.append(f"{running_n} running")
        if waiting_n:
            counts.append(f"{waiting_n} waiting")
        lines: list[str] = [
            f"[work-header]WORK[/] [work-count]({' · '.join(counts)})[/]"
        ]
        for item in active:
            lines.extend(self._render_item(item, spin=spin))
        if finished:
            lines.append(f"[work-finished-header]Finished ({len(finished)})[/]")
            for item in finished:
                lines.extend(self._render_item(item, spin=spin, compact=True))
        return "\n".join(lines)

    def _glyph(self, status: str, spin: int) -> str:
        return _running_glyph(spin) if status == "running" else _GLYPH.get(status, "○")

    def _render_item(self, item: _Item, *, spin: int = 0, compact: bool = False) -> list[str]:
        cls = _NODE_CLASS.get(item.status, "work-pending")
        glyph = self._glyph(item.status, spin)
        head = f"[{cls}]{glyph} {item.label}[/]"
        if item.kind == "subagent" and item.detail:
            head += f" [work-count]{item.detail}[/]"
        out = [head]
        if item.kind == "workflow" and item.status == "needs_input":
            out.append("  [work-needs-input]waiting for your reply in chat[/]")
            if item.detail:
                # First line of the questions, markup-escaped (LLM text may
                # contain literal brackets that Rich would parse as tags).
                first = escape(item.detail.strip().splitlines()[0][:80])
                out.append(f"  [work-count]{first}[/]")
        if not compact and item.kind == "workflow":
            for node in item.nodes:
                out.extend(self._render_node(node, indent=1, spin=spin))
        return out

    def _render_node(self, node: dict, indent: int, spin: int = 0) -> list[str]:
        """Render one workflow node, recursing into nested parallel ``branches``."""
        n_status = str(node.get("status") or "running")
        n_cls = _NODE_CLASS.get(n_status, "work-pending")
        n_glyph = self._glyph(n_status, spin)
        route = node.get("route_label")
        suffix = f" [work-count]{route}[/]" if route else ""
        if n_status == "running":
            suffix += self._render_running_detail(node)
        pad = "  " * indent
        out = [f"{pad}[{n_cls}]{n_glyph} {node.get('label', '?')}[/]{suffix}"]
        for branch in node.get("branches") or []:
            out.extend(self._render_node(branch, indent + 1, spin=spin))
        return out

    def _render_running_detail(self, node: dict) -> str:
        """Round and current-activity suffix for a running node.

        ``round``/``max_rounds`` count the agent's turns within this one visit —
        a different axis from the node's ``iteration``/``budget`` (how many times
        the graph has re-entered the node), so the two are never mixed together
        here. ``activity`` is the tool in flight, as ``{tool, target}``. Every
        field is optional — nested sub-workflow frames, branch frames, and older
        emitters may omit any of them — so each segment is skipped, not rendered
        blank, when absent.
        """
        segments: list[str] = []
        round_, max_rounds = node.get("round"), node.get("max_rounds")
        if round_ is not None and max_rounds is not None:
            segments.append(f"round {round_}/{max_rounds}")
        activity = node.get("activity") or {}
        tool = activity.get("tool")
        if tool:
            target = activity.get("target")
            # ``target`` is arbitrary text from the run (a path, a shell command,
            # a search query) and may contain literal brackets Rich would
            # otherwise parse as markup tags.
            segments.append(f"{tool} {escape(target)}" if target else str(tool))
        if not segments:
            return ""
        return " [work-count]· " + " · ".join(segments) + "[/]"
