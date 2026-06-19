"""always_on distillation (A4, design §2.11).

The agent authors feedback entities (stance / practice / feedback) as the user
gives standing guidance. THIS dream pass curates which of them are injected into
EVERY prompt (the pinned "Always-on guidance" block, ``principal.build_pinned_
context``): an LLM judge ranks them and drops contradictions, then the survivors
are fitted into a token budget (``memory.dream.always_on_token_budget``). The
selected refs get ``always_on=true``; the rest ``always_on=false``. No entity is
ever deleted — only the flag changes — so a pruned item returns automatically
when the budget frees up or a contradiction is resolved.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from durin.memory.entity_page import EntityPage
from durin.memory.principal import _render_pinned_block, mark_always_on
from durin.utils.helpers import estimate_text_tokens

__all__ = ["run_always_on_pass", "FEEDBACK_TYPES"]

# Entity types that are candidates for the always-on pin (design §2.11:
# "stance / practice" feedback). Open vocabulary elsewhere, but the pin is
# behavioural guidance only — never facts like company:/person:.
FEEDBACK_TYPES = ("stance", "practice", "feedback")

LLMInvoke = Callable[..., Any]

_RANK_PROMPT = """You are curating durin's ALWAYS-ON guidance: standing \
behavioural instructions injected into EVERY prompt. Below are candidate items, \
each as a ref line followed by its text. Return the refs in PRIORITY order \
(most load-bearing first), ONE PER LINE, and DROP any item that CONTRADICTS a \
higher-priority item (keep the one that better reflects the user's standing \
intent). Output ONLY refs, one per line — no prose, no numbering.

{items}
"""


def _emit(event: str, **data: Any) -> None:
    try:
        from durin.agent.tools._telemetry import emit_tool_event
        emit_tool_event(event, data)
    except Exception:  # pragma: no cover — telemetry must never break the dream
        pass


def _updated_key(page: EntityPage) -> float:
    """Recency sort key (newer first). Missing timestamp sorts oldest."""
    ts = getattr(page, "updated_at", None)
    try:
        return -ts.timestamp() if ts else 0.0
    except Exception:  # noqa: BLE001
        return 0.0


def _rank(candidates: list[tuple], llm_invoke: LLMInvoke | None, model: str | None) -> list[str]:
    """Refs best-first, contradictions dropped. Uses the LLM judge when given;
    falls back to precedence (user_authored > agent) then recency."""
    valid = {c[0] for c in candidates}
    if llm_invoke is not None and len(candidates) > 1:
        items = "\n\n".join(f"{ref}\n{rendered}" for ref, _p, rendered, _t in candidates)
        prompt = _RANK_PROMPT.format(items=items)
        try:
            resp = llm_invoke(prompt, model=model) if model else llm_invoke(prompt)
            text = resp.text if hasattr(resp, "text") else str(resp)
            seen: set[str] = set()
            out: list[str] = []
            for ln in text.splitlines():
                r = ln.strip().lstrip("-* ").strip()
                if r in valid and r not in seen:
                    seen.add(r)
                    out.append(r)
            if out:
                return out
        except Exception as exc:  # noqa: BLE001
            logger.warning("always_on rank LLM failed; using fallback: {}", exc)
    # Fallback: user-authored first, then most-recently-updated.
    return [
        c[0]
        for c in sorted(
            candidates,
            key=lambda c: (0 if c[1].author == "user_authored" else 1, _updated_key(c[1])),
        )
    ]


def run_always_on_pass(
    workspace: Path,
    *,
    token_budget: int = 1500,
    types: tuple[str, ...] = FEEDBACK_TYPES,
    llm_invoke: LLMInvoke | None = None,
    model: str | None = None,
) -> dict:
    """Distill the always-on pin: rank feedback (drop contradictions), fit the
    token budget, mark the survivors ``always_on``. Returns counts + telemetry.
    """
    t0 = time.perf_counter()
    root = Path(workspace) / "memory" / "entities"
    candidates: list[tuple[str, EntityPage, str, int]] = []
    for t in types:
        d = root / t
        if not d.is_dir():
            continue
        for md in sorted(d.glob("*.md")):
            page = EntityPage.from_file(md)
            if page is None:
                continue
            ref = f"{t}:{md.stem}"
            rendered = _render_pinned_block(page)
            candidates.append((ref, page, rendered, estimate_text_tokens(rendered)))

    if not candidates:
        return {"selected": 0, "pruned": 0, "dropped": 0, "tokens": 0, "changed": 0}

    ranked = _rank(candidates, llm_invoke, model)
    dropped = len(candidates) - len(ranked)  # contradictions the judge omitted
    by_ref = {c[0]: c for c in candidates}

    selected: list[str] = []
    used = 0
    for ref in ranked:
        c = by_ref.get(ref)
        if c is None:
            continue
        # budget is a hard ceiling; 0 disables the pin (nothing fits). A later,
        # smaller item may still fit, so we skip (not break) on overflow.
        if used + c[3] > token_budget:
            continue
        selected.append(ref)
        used += c[3]
    selected_set = set(selected)

    changed = 0
    for ref, page, _r, _t in candidates:
        is_on = ref in selected_set
        if bool(page.attributes.get("always_on")) != is_on:
            mark_always_on(workspace, ref, is_on)
            changed += 1

    out = {
        "selected": len(selected),
        "pruned": len(ranked) - len(selected),  # ranked but didn't fit the budget
        "dropped": dropped,                     # dropped by the contradiction judge
        "tokens": used,
        "changed": changed,
        "duration_ms": int((time.perf_counter() - t0) * 1000),
    }
    _emit("memory.dream.always_on", **{k: v for k, v in out.items() if k != "changed"})
    logger.info(
        "always_on dream: {} pinned ({} tok / {} budget), {} pruned (budget), "
        "{} dropped (contradiction), {} flags changed",
        out["selected"], out["tokens"], token_budget, out["pruned"],
        out["dropped"], changed,
    )
    return out
