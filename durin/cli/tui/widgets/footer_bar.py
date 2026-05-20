"""FooterBar — persistent status line.

Reuses ``durin.cli.footer.build_footer_text`` so the data layer is
identical to the legacy CLI footer. The render is plain Rich markup
(Textual renders Rich markup natively in Static).
"""

from __future__ import annotations

from typing import Any, Callable

from textual.reactive import reactive
from textual.widgets import Static

from durin.cli.footer import build_footer_text

__all__ = ["FooterBar"]


class FooterBar(Static):
    """A one-line status footer below the input.

    The constructor accepts a ``payload_getter`` that returns the
    raw footer dict. The widget refreshes on a timer (default 2s) and
    on every explicit :meth:`refresh_now` call (e.g. after a session
    switch or a model change).
    """

    DEFAULT_CSS = """
    FooterBar {
        height: 1;
        padding: 0 1;
        background: $surface;
        color: $text-muted;
    }
    """

    text: reactive[str] = reactive("")

    def __init__(
        self,
        *,
        payload_getter: Callable[[], dict[str, Any] | None],
        refresh_interval: float = 2.0,
    ) -> None:
        super().__init__()
        self._payload_getter = payload_getter
        self._refresh_interval = max(0.5, refresh_interval)

    def on_mount(self) -> None:
        self.refresh_now()
        self.set_interval(self._refresh_interval, self.refresh_now)

    def refresh_now(self) -> None:
        try:
            payload = self._payload_getter() or {}
        except Exception:  # noqa: BLE001
            self.text = ""
            return
        self.text = _render(payload)

    def watch_text(self, _old: str, new: str) -> None:
        self.update(new)


def _render(p: dict[str, Any]) -> str:
    """Render footer payload as Rich markup (same shape as the legacy footer)."""
    if not p:
        return ""

    session_label = str(p.get("session_key") or "?")
    display_name = p.get("display_name") or ""
    if display_name:
        session_label = f"{display_name} ({session_label})"

    ctx_window = int(p.get("context_window") or 0)
    token_est = int(p.get("token_estimate") or 0)
    if ctx_window:
        pct = int(p.get("context_pct") or 0)
        token_part = f"~{token_est:,}/{ctx_window:,} ({pct}%)"
    else:
        token_part = f"~{token_est:,} tokens"

    vec_glyph = "vec✓" if p.get("vec_index") else "vec✗"
    model = p.get("model", "?")
    preset = p.get("preset", "default")
    mem_count = int(p.get("mem_count") or 0)

    return (
        f"[cyan]{session_label}[/cyan] · "
        f"[green]{model}[/green] ({preset}) · "
        f"{token_part} · mem:{mem_count} {vec_glyph}"
    )


def payload_from_loop(agent_loop: Any, cli_channel: str, cli_chat_id: str) -> dict[str, Any] | None:
    """Convenience adapter from the legacy footer module's payload builder.

    Importable so the App can pass a closure to :class:`FooterBar`.
    """
    if agent_loop is None:
        return None
    try:
        return build_footer_text(agent_loop, cli_channel, cli_chat_id)
    except Exception:  # noqa: BLE001
        return None
