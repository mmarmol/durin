"""Persistent footer shown below the interactive CLI prompt.

prompt_toolkit's ``bottom_toolbar`` parameter takes a callable that
returns a formatted-text-like value, evaluated on every redraw. We
build the footer string from cheap, on-disk-only signals so the
redraw never blocks: session id, model + preset, rough token estimate,
memory entry count, and whether the vector index has been built yet.

The token estimate is intentionally rough (msg count × heuristic) so
this module needs no LLM calls. The real number lives in
``Consolidator.estimate_session_prompt_tokens`` and surfaces in
``/status`` — we treat the footer as continuous *navigation* info, not
billing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from prompt_toolkit.formatted_text import HTML

from durin.utils.helpers import estimate_message_tokens

__all__ = ["build_footer_text", "build_footer_html"]


def _memory_summary(workspace: Path) -> tuple[int, bool]:
    """Return (count of memory/<class>/*.md entries, vector-index-present)."""
    mem_dir = workspace / "memory"
    if not mem_dir.is_dir():
        return 0, False
    count = 0
    for path in mem_dir.rglob("*.md"):
        if any(part.startswith(".") for part in path.parts):
            continue
        count += 1
    vec_present = (mem_dir / ".index.lance").exists()
    return count, vec_present


def _token_estimate(session: Any) -> int:
    """Real per-message token count via tiktoken (no LLM call).

    Uses the same ``estimate_message_tokens`` helper the consolidator uses
    for its budget math, so the footer reflects the same number the
    pre-emptive compaction threshold sees. tiktoken is already a durin
    dep; ~5-10 ms total for a few hundred messages.
    """
    if session is None:
        return 0
    msgs = getattr(session, "messages", []) or []
    total = 0
    try:
        for msg in msgs:
            if isinstance(msg, dict):
                total += int(estimate_message_tokens(msg))
    except Exception:  # noqa: BLE001
        # tiktoken-free environment or unexpected content shape — fall back to
        # a coarse character-based estimate so the footer is never blank.
        chars = 0
        for msg in msgs:
            if isinstance(msg, dict):
                content = msg.get("content")
                if isinstance(content, str):
                    chars += len(content)
        return chars // 4  # ~4 chars per token
    return total


def build_footer_text(
    agent_loop: Any,
    cli_channel: str,
    cli_chat_id: str,
) -> dict[str, Any]:
    """Compute the footer payload as a plain dict.

    Returned keys: ``session_key``, ``display_name``, ``model``,
    ``preset``, ``msg_count``, ``token_estimate``, ``context_window``,
    ``context_pct``, ``mem_count``, ``vec_index``. The caller (or
    :func:`build_footer_html`) turns it into a renderable string.
    """
    session_key = f"{cli_channel}:{cli_chat_id}"
    workspace = Path(getattr(agent_loop, "workspace", "."))

    session = None
    try:
        session = agent_loop.sessions.get_or_create(session_key)
    except Exception:  # noqa: BLE001
        pass

    msg_count = len(getattr(session, "messages", []) or []) if session else 0
    token_est = _token_estimate(session)
    ctx_window = int(getattr(agent_loop, "context_window_tokens", 0) or 0)
    ctx_pct = (token_est * 100 // ctx_window) if ctx_window else 0

    mem_count, vec_present = _memory_summary(workspace)

    display_name = ""
    if session is not None:
        try:
            display_name = (session.metadata or {}).get("display_name", "") or ""
        except Exception:  # noqa: BLE001
            display_name = ""

    # Composition + cache snapshots — populated after the first turn.
    # Format: conv/infra pcts of the last prompt build; cache pct of
    # the last LLM call. None when no turn has run yet.
    cache_pct: int | None = None
    conv_pct: int | None = None
    infra_pct: int | None = None
    try:
        cache_payload = getattr(agent_loop, "_last_cache_usage", None)
        if cache_payload:
            cache_pct = int(cache_payload.get("cache_ratio_pct", 0))
    except Exception:  # noqa: BLE001
        pass
    try:
        comp_payload = getattr(getattr(agent_loop, "context", None), "last_composition", None)
        if comp_payload:
            from durin.agent.context import summarize_composition

            summary = summarize_composition(comp_payload)
            total = summary["total"]
            if total > 0:
                conv_pct = 100 * summary["conversation_tokens"] // total
                infra_pct = 100 * summary["infra_tokens"] // total
    except Exception:  # noqa: BLE001
        pass

    return {
        "session_key": session_key,
        "display_name": display_name,
        "model": getattr(agent_loop, "model", "?") or "?",
        "preset": getattr(agent_loop, "model_preset", None) or "default",
        "msg_count": msg_count,
        "token_estimate": token_est,
        "context_window": ctx_window,
        "context_pct": ctx_pct,
        "mem_count": mem_count,
        "vec_index": vec_present,
        "workspace": str(workspace),
        "cache_pct": cache_pct,
        "conv_pct": conv_pct,
        "infra_pct": infra_pct,
    }


def build_footer_html(payload: dict[str, Any]) -> HTML:
    """Render the footer dict as a prompt_toolkit HTML formatted-text fragment."""
    session_label = payload["session_key"]
    if payload["display_name"]:
        session_label = f"{payload['display_name']} ({session_label})"

    if payload["context_window"]:
        token_part = (
            f"~{payload['token_estimate']:,}/"
            f"{payload['context_window']:,} "
            f"({payload['context_pct']}%)"
        )
    else:
        token_part = f"~{payload['token_estimate']:,} tokens"

    vec_glyph = "vec✓" if payload["vec_index"] else "vec✗"

    # Optional composition + cache snippets — only shown once we have
    # the data (post first turn). Keeps the footer terse on session boot.
    extras: list[str] = []
    if payload.get("cache_pct") is not None:
        extras.append(f"cache:{payload['cache_pct']}%")
    if payload.get("conv_pct") is not None and payload.get("infra_pct") is not None:
        extras.append(f"conv:{payload['conv_pct']}%")
        extras.append(f"infra:{payload['infra_pct']}%")
    extras_str = (" · " + " · ".join(extras)) if extras else ""

    return HTML(
        f"<ansicyan>{session_label}</ansicyan>"
        f" · <ansigreen>{payload['model']}</ansigreen>"
        f" ({payload['preset']})"
        f" · {token_part}"
        f" · mem:{payload['mem_count']} {vec_glyph}"
        f"{extras_str}"
    )
