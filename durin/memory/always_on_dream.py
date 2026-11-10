"""always_on distillation.

The agent authors feedback entities (stance / practice / feedback) as the user
gives standing guidance. THIS dream pass curates which of them are injected into
EVERY prompt (the pinned "Always-on guidance" block, ``principal.build_pinned_
context``): the candidates are ranked, then fitted in rank order into a token
budget (``memory.dream.always_on_token_budget``). The selected refs get
``always_on=true``; the rest ``always_on=false``. No entity is ever deleted —
only the flag changes — so a pruned item returns automatically when the budget
frees up or a contradiction is resolved.

Ranking: when the caller supplies a model call, a judge orders the candidates
and drops any that contradicts a higher one. Without one the order is
user-written pages first, then the most recently updated. Setting or clearing
the flag is a curation write that moves neither, so a run over unchanged pages
selects the same set and flips nothing.

A page seeded from an ingested document is not a candidate: the pin carries
rules stated in conversation, not what a document says.
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

# Entity types that are candidates for the always-on pin
# (stance / practice feedback). Open vocabulary elsewhere, but the pin is
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


def _from_reference(entry: Any) -> bool:
    """Whether a provenance entry names a reference file as its source."""
    return isinstance(entry, dict) and str(
        entry.get("source_ref") or "").startswith("[[references/")


def _seeded_from_document(page: EntityPage) -> bool:
    """True when the page holds what a document says, not a rule a
    conversation taught.

    The document pass stamps every page it writes with a ``derived_from``
    link whose recorded source is the reference file itself; a link the
    agent adds, or the pass that links a session's pages to the documents
    read in it, is sourced from a session. The document pass also writes to
    a page that already exists when the document covers the same subject,
    so the link alone is not enough: the body, when there is one, must have
    been written from a reference file too. A rule stated in conversation
    keeps its conversational body and stays a candidate.
    """
    prov = page.provenance or {}
    links = prov.get("derived_from")
    links = links if isinstance(links, dict) else {}
    if not any(_from_reference(links.get(ref)) for ref in page.derived_from):
        return False
    return not (page.body or "").strip() or _from_reference(prov.get("body"))


def _rank(candidates: list[tuple], llm_invoke: LLMInvoke | None, model: str | None) -> list[str]:
    """Refs best-first. With ``llm_invoke``, a judge orders them and drops
    contradictions; without it, or when the judge fails, the order is
    user-written pages first, then the most recently updated."""
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
    """Distill the always-on pin: rank the candidates (``_rank``), fit the
    token budget, mark the survivors ``always_on`` and unmark every other page
    of ``types``. Returns counts + telemetry; ``changed`` is the number of
    flags the run set or cleared.
    """
    t0 = time.perf_counter()
    root = Path(workspace) / "memory" / "entities"
    candidates: list[tuple[str, EntityPage, str, int]] = []
    # Pages of these types that are not candidates. One pinned by an earlier
    # run still has to lose its flag.
    excluded: list[tuple[str, EntityPage]] = []
    for t in types:
        d = root / t
        if not d.is_dir():
            continue
        for md in sorted(d.glob("*.md")):
            page = EntityPage.from_file(md)
            if page is None:
                continue
            ref = f"{t}:{md.stem}"
            if _seeded_from_document(page):
                excluded.append((ref, page))
                continue
            rendered = _render_pinned_block(page)
            candidates.append((ref, page, rendered, estimate_text_tokens(rendered)))

    if not candidates and not excluded:
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
    for ref, page in [(c[0], c[1]) for c in candidates] + excluded:
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
    _emit("memory.dream.always_on", **out)
    logger.info(
        "always_on dream: {} pinned ({} tok / {} budget), {} pruned (budget), "
        "{} dropped (contradiction), {} flags changed",
        out["selected"], out["tokens"], token_budget, out["pruned"],
        out["dropped"], changed,
    )
    return out
