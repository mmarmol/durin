"""FooterBar — persistent status line, pi-style.

Pi shows: ``~/Development/pi.dev (main) ↑21k ↓679 R161k $0.000 (sub) 18.4%/128k (auto)   grok-code-fast-1 • thinking off``

We emit a comparable shape:
``<cwd> (<branch>) · ↑<input>k · <ctx%>/<window>k · <model> · think:<state>``

The widget refreshes on a timer (default 2s) and on every explicit
``refresh_now()`` call (after a session switch or model change).
"""

from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

from textual.reactive import reactive
from textual.widgets import Static

from durin.cli.footer import build_footer_text

__all__ = ["FooterBar"]


class FooterBar(Static):
    DEFAULT_CSS = """
    FooterBar {
        height: 1;
        padding: 0 2;
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


def _format_path(p: str) -> str:
    """Collapse $HOME → ~ and trim long workspace prefixes for readability."""
    if not p:
        return "?"
    try:
        home = str(Path.home())
        if p.startswith(home):
            p = "~" + p[len(home):]
    except Exception:
        pass
    return p


@lru_cache(maxsize=8)
def _git_branch(path: str) -> str:
    """Return the current git branch for ``path``, or empty if not a git repo.

    Cached because branch changes are rare and `git` is slow enough that
    polling it every footer tick (every 2s) is wasteful.
    """
    try:
        result = subprocess.run(
            ["git", "-C", path, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=0.5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:  # noqa: BLE001
        pass
    return ""


def _fmt_k(n: int) -> str:
    """Compact thousands: 21_000 → '21k', 161_280 → '161k'."""
    if n < 1000:
        return str(n)
    if n < 10_000:
        return f"{n / 1000:.1f}k"
    return f"{n // 1000}k"


def _render(p: dict[str, Any]) -> str:
    if not p:
        return ""

    session_label = str(p.get("session_key") or "?")
    display_name = p.get("display_name") or ""
    if display_name:
        session_label = display_name

    workspace = _format_path(str(p.get("workspace") or ""))
    # Try to derive a branch from the raw (unformatted) workspace path.
    raw_workspace = str(p.get("workspace") or "")
    branch = _git_branch(raw_workspace) if raw_workspace else ""
    branch_part = f" [dim]({branch})[/dim]" if branch else ""

    token_est = int(p.get("token_estimate") or 0)
    ctx_window = int(p.get("context_window") or 0)
    if ctx_window:
        pct = int(p.get("context_pct") or 0)
        ctx_part = f"[dim]{pct}%[/dim]/[dim]{_fmt_k(ctx_window)}[/dim]"
    else:
        ctx_part = f"~{_fmt_k(token_est)} tokens"

    model = p.get("model", "?")

    # Optional turn-level snippets. Only rendered once data exists
    # (post first LLM round-trip / first prompt build) so the footer
    # stays compact on cold start. Sourced from the cached
    # ``cache.usage`` and ``context.composition`` payloads on the loop.
    extras: list[str] = []
    cache_pct = p.get("cache_pct")
    if cache_pct is not None:
        extras.append(f"[dim]cache:[/dim]{cache_pct}%")
    conv_pct = p.get("conv_pct")
    infra_pct = p.get("infra_pct")
    if conv_pct is not None and infra_pct is not None:
        extras.append(f"[dim]conv:[/dim]{conv_pct}%")
        extras.append(f"[dim]infra:[/dim]{infra_pct}%")
    extras_part = (" · " + " · ".join(extras)) if extras else ""

    mode = p.get("mode")
    mode_part = f" · [bold]{mode}[/bold] ·" if mode else ""

    # Live turn diagnostics. While a turn is in flight the footer shows a
    # ticking elapsed clock and hides the previous turn's latency (both at
    # once would read as two contradicting timers); a provider retry in
    # progress rides alongside so a silent backoff doesn't look like a hang.
    elapsed_s = p.get("elapsed_s")
    if isinstance(elapsed_s, (int, float)):
        m, s = divmod(int(elapsed_s), 60)
        clock = f"{m}:{s:02d}" if m else f"{s}s"
        elapsed_part = f" · [bold]● {clock}[/bold]"
        latency_part = ""
    else:
        elapsed_part = ""
        latency_ms = p.get("latency_ms")
        latency_part = (
            f" · ⏱ {latency_ms / 1000:.1f}s"
            if isinstance(latency_ms, (int, float)) and latency_ms > 0
            else ""
        )
    retry_part = _render_retry(p.get("retry_status"))

    # Footer is for *current-conversation* state. Memory totals & vector
    # availability are install-level info — they belong in the startup
    # banner, not in a per-tick status line.
    return (
        f"[cyan]{workspace}[/cyan]{branch_part} · "
        f"↑{_fmt_k(token_est)} · "
        f"{ctx_part} · "
        f"[green]{model}[/green] · "
        f"[dim]{session_label}[/dim]"
        f"{extras_part}"
        f"{mode_part}"
        f"{latency_part}"
        f"{elapsed_part}"
        f"{retry_part}"
    )


def _render_retry(status: Any) -> str:
    """Footer segment for a provider retry in progress.

    ``⟳ llm retry 2/10 · 14s`` while backing off (attempt / limit / seconds
    until the next try; persistent mode has no limit → ``∞``), or
    ``✗ llm giving up`` on the final attempt — the error text itself follows
    in chat, this only explains why the turn stalled.
    """
    if not isinstance(status, dict):
        return ""
    attempt = status.get("attempt")
    if status.get("final"):
        return f" · [red]✗ llm giving up (attempt {attempt})[/red]"
    max_attempts = status.get("max_attempts")
    limit = "∞" if status.get("persistent") or not max_attempts else str(max_attempts)
    delay = int(status.get("delay_s") or 0)
    return f" · [yellow]⟳ llm retry {attempt}/{limit} · {delay}s[/yellow]"


def payload_from_loop(agent_loop: Any, cli_channel: str, cli_chat_id: str) -> dict[str, Any] | None:
    """Convenience adapter from the legacy footer module's payload builder."""
    if agent_loop is None:
        return None
    try:
        return build_footer_text(agent_loop, cli_channel, cli_chat_id)
    except Exception:  # noqa: BLE001
        return None
