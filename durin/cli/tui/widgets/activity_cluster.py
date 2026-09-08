"""ActivityCluster — collapsible container for reasoning + tool bubbles.

During an agent turn, reasoning steps and tool calls flood the chat with
intermediate output. This widget groups them into a single collapsible
section with a live summary header, mirroring the webui's
``AgentActivityCluster.tsx``.

Lifecycle:
    1. Created when the first reasoning delta or tool event arrives.
    2. Child bubbles (MessageBubble/ToolCallBubble) are mounted inside it.
    3. ``finalize()`` is called when assistant text starts streaming —
       the header switches from "Working…" to "Done" and the body collapses.
    4. The user can expand/collapse at any time.
"""

from __future__ import annotations

from textual.containers import Vertical
from textual.reactive import reactive
from textual.widgets import Label

__all__ = ["ActivityCluster"]


class ActivityCluster(Vertical):
    """Collapsible group of reasoning + tool-call bubbles.

    The header is a Label that updates as steps arrive.
    The body is a Vertical container that holds the actual bubbles.
    """

    DEFAULT_CSS = """
    ActivityCluster {
        margin: 0 1 0 1;
        padding: 0;
        border-left: outer $accent 50%;
        height: auto;
    }
    ActivityCluster > #cluster-header {
        height: 1;
        padding: 0 1;
        color: $text-muted;
        text-style: bold;
        background: $surface-lighten-1;
    }
    ActivityCluster.collapsed > MessageBubble {
        display: none;
    }
    ActivityCluster.collapsed > ToolCallBubble {
        display: none;
    }
    ActivityCluster.-finalized > #cluster-header {
        color: $text-muted;
        text-style: italic;
    }
    """

    collapsed: reactive[bool] = reactive(False)

    def __init__(self) -> None:
        super().__init__()
        self._reasoning_count = 0
        self._tool_count = 0
        self._finalized = False

    def compose(self) -> object:
        yield Label("", id="cluster-header")

    def on_mount(self) -> None:
        self._update_header()

    def on_click(self) -> None:
        """Toggle collapse on click."""
        self.collapsed = not self.collapsed

    def watch_collapsed(self, collapsed: bool) -> None:
        if collapsed:
            self.add_class("collapsed")
        else:
            self.remove_class("collapsed")
        self._update_header()

    def add_reasoning_step(self) -> None:
        """Increment the reasoning step counter and refresh header."""
        self._reasoning_count += 1
        self._update_header()

    def add_tool_step(self) -> None:
        """Increment the tool-call counter and refresh header."""
        self._tool_count += 1
        self._update_header()

    def remove_tool_step(self) -> None:
        """Decrement the tool-call counter and refresh header.

        Mirrors add_tool_step(): called when a bubble that was already
        counted gets removed instead of finalised (e.g. an empty memory
        recall dropped by the caller), so the collapsed header doesn't
        keep claiming a tool ran with nothing left inside to show for it.
        """
        self._tool_count = max(0, self._tool_count - 1)
        self._update_header()

    def is_empty(self) -> bool:
        """True once every reasoning/tool step this cluster ever held has
        been removed again — used to drop the cluster itself instead of
        leaving a bordered shell with nothing inside (e.g. an empty memory
        recall that was a turn's only activity)."""
        return self._tool_count == 0 and self._reasoning_count == 0

    def finalize(self) -> None:
        """Mark the cluster as done — switch header to summary, collapse."""
        self._finalized = True
        self.add_class("-finalized")
        self.collapsed = True
        self._update_header()

    def _update_header(self) -> None:
        try:
            label = self.query_one("#cluster-header", Label)
        except Exception:  # noqa: BLE001
            return
        marker = "✓" if self._finalized else "⠋"
        state = "Done" if self._finalized else "Working…"
        parts: list[str] = [f"{marker} {state}"]
        if self._reasoning_count:
            parts.append(f"{self._reasoning_count} reasoning")
        if self._tool_count:
            tool_word = "tool" if self._tool_count == 1 else "tools"
            parts.append(f"{self._tool_count} {tool_word}")
        hint = " · click to expand" if self.collapsed and self._finalized else ""
        label.update(" · ".join(parts) + hint)
