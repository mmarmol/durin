"""Memory-related onboarding questions.

This module ships the **wizard question text** for the memory
subsystem's opt-in features. The full wizard integration (top-level
"Memory" menu in ``durin onboard``) is Phase 6 of the memory roadmap;
for Phase 1 we land the canonical question text and a callable that
delivers it, so the feature is reachable from anywhere that imports
this module (the install script, the web onboarding flow, ad-hoc CLI
helpers, …).

Q6.3 — auto-absorb opt-in — is the only memory question that depends
on a feature shipping in Phase 1 (the absorb-judge step at
``durin/memory/absorb_judge.py``). The other onboarding questions
(memory enable, cross-encoder, aux model) land alongside their own
features in Phases 6, 4, and 6 respectively.
"""

from __future__ import annotations

from textwrap import dedent
from typing import Any

__all__ = [
    "AUTO_ABSORB_QUESTION_TEXT",
    "AUX_MODEL_QUESTION_TEXT",
    "CROSS_ENCODER_QUESTION_TEXT",
    "MEMORY_ENABLE_QUESTION_TEXT",
    "prompt_enable_auto_absorb",
    "prompt_enable_cross_encoder",
    "prompt_enable_memory_subsystem",
    "prompt_memory_aux_model",
]


# Verbatim from `docs/memory/06_prompts_and_instructions.md` §6.3.
AUTO_ABSORB_QUESTION_TEXT: str = dedent(
    """\
    After Dream consolidates a batch of observations, it can optionally
    run an LLM judge over entity pairs that share aliases (e.g.,
    "Marcelo Marmol" and "M. Marmol") and merge them when the judge is
    highly confident.

    This is OFF by default because a bad merge silently combines two
    distinct entities — recovery requires `git revert` in the memory
    repo. Enable only when you trust the judge model and want to
    reduce manual cleanup.

    Defaults when enabled:
      - Confidence threshold: 95/100 (high — favors precision)
      - Minimum age: 24h (prevents Dream from merging its own hallucinations)
      - Judge model: uses your Dream consolidator model

    Enable auto-absorb now? [y/N]:
    """
)


# Verbatim from `docs/memory/06_prompts_and_instructions.md` §6.1.
MEMORY_ENABLE_QUESTION_TEXT: str = dedent(
    """\
    durin's memory system lets the agent remember facts across sessions.
    Enabling it downloads a small embedding model (~120MB) and starts the
    local consolidation process.

    Enable memory? [Y/n]:
    """
)


def prompt_enable_memory_subsystem(current: bool = True) -> bool:
    """Q6.1 — defaults to True per spec.

    Same idempotency contract as the other prompts: re-prompts
    preserve the previous opt-out, aborts (Ctrl+C) preserve current.
    """
    questionary = _get_questionary()
    answer: Any = questionary.confirm(
        MEMORY_ENABLE_QUESTION_TEXT,
        default=bool(current),
    ).ask()
    if answer is None:
        return bool(current)
    return bool(answer)


# Verbatim from `docs/memory/06_prompts_and_instructions.md` §6.4.
AUX_MODEL_QUESTION_TEXT: str = dedent(
    """\
    durin's Dream process consolidates memory using an LLM. It runs in
    the background, consuming ~$0.25-$1.00/day for an active workspace.
    You can use the same model as your main agent, or a separate one
    for memory tasks.

    Memory model: [same as agent / specify / skip]
    """
)


def prompt_memory_aux_model(
    *,
    agent_model: str,
    current: Optional[str] = None,
) -> Optional[str]:
    """Q6.4 — three-way choice: same / specify / skip.

    Returns:
        - ``agent_model`` when the user picks "same".
        - A user-supplied model id when the user picks "specify".
        - ``None`` when the user picks "skip" (Dream uses default).

    Ctrl+C-style abort preserves the *current* value (or ``None`` if
    the user has never set one).
    """
    questionary = _get_questionary()
    choices = ["same as agent", "specify", "skip"]
    selected: Any = questionary.select(
        AUX_MODEL_QUESTION_TEXT,
        choices=choices,
        default=choices[0],
    ).ask()
    if selected is None:
        return current
    if selected == "same as agent":
        return agent_model
    if selected == "skip":
        return None
    # "specify"
    typed: Any = questionary.text(
        "Enter the memory model id (e.g. `glm-5.1`, `claude-haiku-4-5`):",
        default=current or "",
    ).ask()
    if typed is None or not str(typed).strip():
        return current
    return str(typed).strip()


# Verbatim from `docs/memory/06_prompts_and_instructions.md` §6.2.
CROSS_ENCODER_QUESTION_TEXT: str = dedent(
    """\
    Advanced retrieval option: durin can use a cross-encoder reranker
    to improve search quality. This adds 300-1500ms latency per query
    (depending on the model) and requires ~1GB additional RAM. The
    default model is `jinaai/jina-reranker-v2-base-multilingual`
    (multilingual, covers 100+ languages including CJK).

    Most users do NOT need this — the default search (without the
    reranker) works well for typical workloads. Enable it later via
    the web dashboard if you find queries returning poor results.

    Enable advanced reranker now? [y/N]:
    """
)


def prompt_enable_cross_encoder(current: bool = False) -> bool:
    """Render the Q6.2 question and return the user's choice.

    Same pattern as :func:`prompt_enable_auto_absorb`: re-prompts
    preserve the prior opt-in; Ctrl+C preserves *current*.
    """
    questionary = _get_questionary()
    answer: Any = questionary.confirm(
        CROSS_ENCODER_QUESTION_TEXT,
        default=bool(current),
    ).ask()
    if answer is None:
        return bool(current)
    return bool(answer)


def prompt_enable_auto_absorb(current: bool = False) -> bool:
    """Render the Q6.3 question and return the user's choice.

    *current* is the existing value of ``memory.dream.auto_absorb.enabled``
    in the live config: used as the ``confirm`` default so a re-prompt
    preserves the user's earlier opt-in. Aborting the prompt (Ctrl+C
    in ``questionary``) also preserves *current*.
    """
    questionary = _get_questionary()
    answer: Any = questionary.confirm(
        AUTO_ABSORB_QUESTION_TEXT,
        default=bool(current),
    ).ask()
    if answer is None:
        return bool(current)
    return bool(answer)


def _get_questionary() -> Any:
    """Lazy import; the wizard depends on the optional ``questionary``
    package the same way the main onboard flow does. Kept in its own
    helper so tests can monkeypatch a fake."""
    try:
        import questionary
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "questionary not installed; durin onboard memory questions "
            "require the optional 'questionary' dependency."
        ) from exc
    return questionary
