"""WorkStore — folds workflow/subagent progress events into a renderable tree.

Push-driven model for the sidebar's WORK section. The agent emits one outbound
event per progress phase (`running`, then `end`/`error`) for each workflow run
and each sub-agent. This store keeps the latest snapshot per item keyed by
``call_id``, splits them into active vs. finished, and renders Textual console
markup. Kept free of Textual imports so it is unit-testable in isolation.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from rich.markup import escape

__all__ = ["WorkStore"]

_GLYPH = {"running": "○", "done": "✓", "failed": "✗", "pending": "○", "needs_input": "?"}
# Braille spinner frames — a running node cycles through these so the panel
# shows motion while work is in flight.
_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def _running_glyph(spin: int) -> str:
    return _SPINNER[spin % len(_SPINNER)]


def _format_elapsed(total_seconds: float) -> str:
    """Format a duration the way a stopwatch reads: ``m:ss``, extending to
    ``h:mm:ss`` once it runs past an hour. Matches the output shape of the web
    UI's ``formatElapsed`` (``webui/src/lib/work-format.ts``) so a duration
    reads the same on both surfaces.
    """
    total = max(0, int(total_seconds))
    s = total % 60
    m = (total // 60) % 60
    h = total // 3600
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


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
                # ``tool`` is text carried straight from the model's tool-call
                # response (the same trust class as a workflow node's activity
                # tool, see ``_render_running_detail`` below) and may contain
                # literal brackets Rich would otherwise parse as markup tags,
                # so it is escaped before entering this detail string.
                detail = (
                    f"iter {prog.get('iteration')} · {escape(str(prog.get('tool')))}"
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

    def render_markup(self, spin: int = 0, now: float | None = None) -> str:
        """Render the WORK section. ``spin`` advances the running-node spinner.

        ``now`` is the instant per-node elapsed time is measured against. It
        defaults to the real current time — resolved once here so every node in
        this render sees the same instant — and is only overridden by tests that
        need a fixed clock instead of the wall clock.
        """
        if self.is_empty():
            return ""
        if now is None:
            now = time.time()
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
            lines.extend(self._render_item(item, spin=spin, now=now))
        if finished:
            lines.append(f"[work-finished-header]Finished ({len(finished)})[/]")
            for item in finished:
                lines.extend(self._render_item(item, spin=spin, compact=True, now=now))
        return "\n".join(lines)

    def _glyph(self, status: str, spin: int) -> str:
        return _running_glyph(spin) if status == "running" else _GLYPH.get(status, "○")

    def _render_item(
        self, item: _Item, *, spin: int = 0, compact: bool = False, now: float
    ) -> list[str]:
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
                out.extend(self._render_node(node, indent=1, spin=spin, now=now))
        return out

    def _render_node(self, node: dict, indent: int, spin: int = 0, *, now: float) -> list[str]:
        """Render one workflow node, recursing into nested parallel ``branches``."""
        n_status = str(node.get("status") or "running")
        n_cls = _NODE_CLASS.get(n_status, "work-pending")
        n_glyph = self._glyph(n_status, spin)
        route = node.get("route_label")
        suffix = f" [work-count]{route}[/]" if route else ""
        elapsed = self._node_elapsed(node, n_status, now)
        if elapsed:
            suffix += f" [work-count]· {elapsed}[/]"
        if n_status == "running":
            suffix += self._render_running_detail(node)
        pad = "  " * indent
        out = [f"{pad}[{n_cls}]{n_glyph} {node.get('label', '?')}[/]{suffix}"]
        for branch in node.get("branches") or []:
            out.extend(self._render_node(branch, indent + 1, spin=spin, now=now))
        return out

    def _node_elapsed(self, node: dict, status: str, now: float) -> str:
        """The elapsed-time text for one node, or ``""`` when there is nothing to show.

        A running node's clock is a live diff of ``started_at`` against ``now`` —
        it keeps advancing on every re-render, not just when a new event arrives,
        since ``now`` is the real wall clock by default. A finished node's
        ``duration_s`` is already a span of seconds, not a timestamp, and is
        formatted as-is. A node reporting neither (not yet started, or from an
        older emitter) shows no time segment at all — unchanged from before these
        fields existed.
        """
        if status == "running":
            started_at = node.get("started_at")
            return _format_elapsed(now - started_at) if started_at is not None else ""
        duration_s = node.get("duration_s")
        return _format_elapsed(duration_s) if duration_s is not None else ""

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
            # Both ``tool`` and ``target`` are text carried straight from the
            # model's tool-call response, not a fixed set of internal names —
            # an unknown or hallucinated tool name reaches here before the
            # runner's own loop guard has a chance to catch it (that guard
            # only trips after the name repeats past a threshold). Either may
            # contain literal brackets Rich would otherwise parse as markup
            # tags, so both are escaped before entering this string.
            tool_text = escape(str(tool))
            segments.append(f"{tool_text} {escape(target)}" if target else tool_text)
        if not segments:
            return ""
        return " [work-count]· " + " · ".join(segments) + "[/]"
