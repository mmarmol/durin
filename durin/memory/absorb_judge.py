"""LLM-judge for entity absorption (doc 25 §2.D).

When auto-absorb is enabled, the :class:`DreamRunner` calls
:func:`judge_pair` on every alias-overlap candidate that survived the
cross-type filter and the 24-hour quarantine. The judge returns a
verdict (``"same"`` / ``"different"`` / ``"unclear"``), a confidence
score (0-100), and free-form reasoning. The dispatcher merges only when
``verdict == "same"`` AND ``confidence >= confidence_threshold``.

Design notes:

- **Adversarial prompt**: alias overlap is treated as input evidence
  (necessary) not as proof (insufficient). The template tells the
  model to default to ``"different"`` when content evidence is thin.
- **Temporal context**: every page block carries ``created_at`` (file
  mtime), ``dream_processed_through`` (cursor), and the page's own
  body. This mitigates self-consistency bias when ``judge_model ==
  dream_model`` (glm peer review C2, 2026-05-24) — the judge can see
  that two pages observed years apart probably aren't the same
  entity even if alias coincides.
- **Markdown markers**: same envelope format as ``consolidator.md``
  (``===VERDICT===`` / ``===CONFIDENCE===`` / ``===REASONING===`` /
  ``===END===``). Keeps the parser surface consistent.
- **Retry on parse failure**: up to ``max_retries`` (default 2)
  attempts. Each retry re-sends the same prompt with no feedback —
  parse failures are usually transient (e.g. the model wrapped the
  output in extra prose).
- **Always succeeds OR raises**: the function returns a populated
  :class:`JudgeResult` or raises :class:`JudgeError`. Callers should
  catch and treat a failure as "skip this candidate".
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from durin.memory.entity_page import EntityPage

__all__ = [
    "JudgeError",
    "JudgeResult",
    "judge_pair",
]

logger = logging.getLogger(__name__)

LLMInvoke = Callable[..., str]

_TEMPLATE_PATH = (
    Path(__file__).parent.parent / "templates" / "dream" / "absorb_judge.md"
)

# Markdown-marker block extraction. Tolerant to whitespace and to extra
# prose the model may add before / after the envelope.
_RE_VERDICT = re.compile(
    r"===VERDICT===\s*(?P<verdict>\S+)\s*===CONFIDENCE===",
    re.IGNORECASE | re.DOTALL,
)
_RE_CONFIDENCE = re.compile(
    r"===CONFIDENCE===\s*(?P<confidence>\d+)\s*===REASONING===",
    re.IGNORECASE | re.DOTALL,
)
_RE_REASONING = re.compile(
    r"===REASONING===\s*(?P<reasoning>.*?)\s*===END===",
    re.IGNORECASE | re.DOTALL,
)

_VALID_VERDICTS = frozenset({"same", "different", "unclear"})


class JudgeError(Exception):
    """Raised when the judge LLM call or output parsing fails after retries."""


@dataclass(frozen=True)
class JudgeResult:
    """One LLM-judge decision for a candidate entity pair.

    ``verdict`` is one of ``"same"`` / ``"different"`` / ``"unclear"``.
    ``confidence`` is the model's self-reported certainty 0-100; the
    dispatcher's threshold check is the operational gate.
    ``reasoning`` is the model's free-form justification (1-3 sentences
    per the prompt), recorded in the absorb commit body so
    ``durin memory history`` shows why the merge happened.
    """

    verdict: str
    confidence: int
    reasoning: str


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def judge_pair(
    canonical: EntityPage,
    absorbed: EntityPage,
    shared_aliases: list[str],
    *,
    llm_invoke: LLMInvoke,
    model: str = "glm-5.1",
    max_retries: int = 2,
    canonical_ref: str | None = None,
    absorbed_ref: str | None = None,
    canonical_mtime: datetime | None = None,
    absorbed_mtime: datetime | None = None,
) -> JudgeResult:
    """Ask the LLM whether two entity pages describe the same identity.

    Returns a :class:`JudgeResult` or raises :class:`JudgeError`. Never
    raises for non-judge reasons (LLM exceptions are caught and
    rewrapped as ``JudgeError`` so the caller has a single failure
    mode).

    ``canonical_ref`` / ``absorbed_ref`` default to ``"<type>:<name>"``
    when omitted; the dispatcher always passes the canonical refs to
    keep the prompt accurate.

    ``canonical_mtime`` / ``absorbed_mtime`` are the file modification
    timestamps used to build the temporal context block. ``None`` falls
    back to "unknown" (the prompt still works without them, just with
    less signal).
    """
    prompt = _build_prompt(
        canonical=canonical,
        absorbed=absorbed,
        shared_aliases=shared_aliases,
        canonical_ref=canonical_ref or f"{canonical.type}:{canonical.name}",
        absorbed_ref=absorbed_ref or f"{absorbed.type}:{absorbed.name}",
        canonical_mtime=canonical_mtime,
        absorbed_mtime=absorbed_mtime,
    )

    # A5: tolerate both new LLMResponse-returning and legacy str-
    # returning llm_invoke shapes. The judge consumes the text; token
    # usage propagation for auto-absorb is intentionally NOT plumbed
    # into `memory.dream.end` (the absorb judge runs AFTER the dream
    # pass and emits its own `memory.absorb.judged` event).
    from durin.memory.dream import LLMResponse as _LLMResponse

    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            response = llm_invoke(prompt, model=model)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            logger.warning(
                "absorb_judge LLM call failed (attempt %d/%d): %s",
                attempt + 1, max_retries + 1, exc,
            )
            continue
        raw = response.text if isinstance(response, _LLMResponse) else str(response)
        try:
            return _parse_response(raw)
        except JudgeError as exc:
            last_error = exc
            logger.warning(
                "absorb_judge parse failed (attempt %d/%d): %s",
                attempt + 1, max_retries + 1, exc,
            )

    raise JudgeError(
        f"absorb_judge failed after {max_retries + 1} attempts: {last_error}"
    )


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------


def _build_prompt(
    *,
    canonical: EntityPage,
    absorbed: EntityPage,
    shared_aliases: list[str],
    canonical_ref: str,
    absorbed_ref: str,
    canonical_mtime: datetime | None,
    absorbed_mtime: datetime | None,
) -> str:
    """Assemble the judge prompt from the template + page blocks."""
    template = _load_template()
    page_a_block = _render_page_block(canonical, mtime=canonical_mtime)
    page_b_block = _render_page_block(absorbed, mtime=absorbed_mtime)
    return template.format(
        shared_aliases=", ".join(shared_aliases) if shared_aliases else "(none)",
        ref_a=canonical_ref,
        ref_b=absorbed_ref,
        page_a_block=page_a_block,
        page_b_block=page_b_block,
    )


def _load_template() -> str:
    """Extract the fenced template body from absorb_judge.md.

    The .md file is a doc that describes the template and embeds it
    inside a ``` block — same pattern as ``consolidator.md``. We grab
    the largest fenced block to avoid accidentally formatting docs prose.
    """
    text = _TEMPLATE_PATH.read_text(encoding="utf-8")
    matches = re.findall(r"```(?:[a-z]*)\n(.*?)\n```", text, re.DOTALL)
    if not matches:
        raise JudgeError(
            f"absorb_judge template at {_TEMPLATE_PATH} has no fenced block"
        )
    return max(matches, key=len)


def _render_page_block(page: EntityPage, *, mtime: datetime | None) -> str:
    """Render one page as a self-contained block for the judge prompt.

    Includes temporal metadata at the top so the judge can reason about
    "are these two the same entity OBSERVED in the same time window".
    """
    lines: list[str] = []
    created = (
        mtime.astimezone(timezone.utc).isoformat()
        if mtime is not None
        else "(unknown)"
    )
    lines.append(f"- File last modified: {created}")
    cursor = page.dream_processed_through
    if cursor:
        lines.append(f"- Last dreamed through: {cursor}")
    if page.aliases:
        lines.append(f"- Aliases: {', '.join(page.aliases)}")
    # Identifiers are richer than aliases for entity disambiguation
    # (email / slack / github are usually globally unique).
    identifiers = page.extra.get("identifiers") if page.extra else None
    if isinstance(identifiers, dict) and identifiers:
        for key, value in identifiers.items():
            if isinstance(value, list):
                rendered = ", ".join(str(v) for v in value)
            else:
                rendered = str(value)
            lines.append(f"- Identifier ({key}): {rendered}")
    lines.append("")
    lines.append(page.body or "(empty body)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Output parsing
# ---------------------------------------------------------------------------


def _parse_response(raw: str) -> JudgeResult:
    """Extract verdict / confidence / reasoning from the markdown markers.

    Raises :class:`JudgeError` if any block is missing or malformed.
    Tolerates: extra prose around the envelope, case variation in the
    verdict, surrounding whitespace.
    """
    if not raw or not isinstance(raw, str):
        raise JudgeError("empty or non-string LLM response")

    verdict_match = _RE_VERDICT.search(raw)
    if verdict_match is None:
        raise JudgeError("missing ===VERDICT=== block")
    verdict = verdict_match.group("verdict").strip().lower()
    if verdict not in _VALID_VERDICTS:
        raise JudgeError(
            f"invalid verdict {verdict!r}; expected one of {sorted(_VALID_VERDICTS)}"
        )

    confidence_match = _RE_CONFIDENCE.search(raw)
    if confidence_match is None:
        raise JudgeError("missing ===CONFIDENCE=== block")
    try:
        confidence = int(confidence_match.group("confidence").strip())
    except ValueError as exc:
        raise JudgeError(f"non-integer confidence: {exc}") from None
    if not 0 <= confidence <= 100:
        raise JudgeError(f"confidence {confidence} out of [0, 100]")

    reasoning_match = _RE_REASONING.search(raw)
    if reasoning_match is None:
        raise JudgeError("missing ===REASONING=== / ===END=== block")
    reasoning = reasoning_match.group("reasoning").strip()
    if not reasoning:
        raise JudgeError("empty reasoning block")

    return JudgeResult(
        verdict=verdict,
        confidence=confidence,
        reasoning=reasoning,
    )
