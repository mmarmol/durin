"""LLM-judge for entity absorption.

When auto-absorb is enabled, the refine pass (``refine_dream.run_refine``)
calls :func:`judge_pair` on every alias-overlap candidate that survived the
cross-type filter and the run-scoped quarantine. The judge returns a
verdict (``"same"`` / ``"different"`` / ``"unclear"``), a confidence
score (0-100), and free-form reasoning. Refine merges only when
``verdict == "same"`` AND ``confidence >= confidence_threshold``.

Design notes:

- **Adversarial prompt**: alias overlap is treated as input evidence
  (necessary) not as proof (insufficient). The template tells the
  model to default to ``"different"`` when content evidence is thin.
- **Temporal context**: every page block carries ``created_at`` (file
  mtime), ``dream_processed_through`` (cursor), and the page's own
  body. This mitigates self-consistency bias when ``judge_model ==
  dream_model`` — the judge can see
  that two pages observed years apart probably aren't the same
  entity even if alias coincides.
- **Markdown markers**: a ``===MARKER===`` envelope format
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

# Envelope extraction. Each block is the text between its marker and the
# next marker (or the end of the reply), so prose between blocks, emphasis
# around a word, a fenced reply, a percent sign or a decimal confidence
# and a missing closing marker all still parse — the model's drift costs
# no extra call. A block that is absent or holds no usable value is still
# an error: the judge never guesses a verdict.
_MARKERS = ("===VERDICT===", "===CONFIDENCE===", "===REASONING===", "===END===")
_RE_MARKER = re.compile("|".join(re.escape(m) for m in _MARKERS), re.IGNORECASE)
_RE_NUMBER = re.compile(r"(\d+(?:\.\d+)?)\s*%?")


def _blocks(raw: str) -> dict[str, str]:
    """``{marker: text after it up to the next marker}`` (markers upper-cased).

    The LAST occurrence of a marker wins: a model that restates the envelope
    (the retry note shows it) answers after the restatement, and reading the
    placeholder instead of the answer would be worse than a parse error.
    """
    found = list(_RE_MARKER.finditer(raw))
    out: dict[str, str] = {}
    for k, m in enumerate(found):
        end = found[k + 1].start() if k + 1 < len(found) else len(raw)
        out[m.group(0).upper()] = raw[m.end():end]
    return out



_VALID_VERDICTS = frozenset({"same", "different", "unclear"})

# Maximum characters of the page's to_markdown() serialization included in
# the judge prompt. Free-text body can grow unboundedly; attributes/relations
# are typically small and always included in full before the cap applies.
_PAGE_BUDGET_CHARS = 6000


class JudgeError(Exception):
    """Raised when the judge LLM call or output parsing fails after retries.

    ``kind`` tells the caller what failed: ``"parse"`` — the model answered
    but not in the envelope (a property of this pair and prompt, worth
    remembering); ``"provider"`` — the call itself failed or the provider
    returned an error after its own retries (a property of the moment, not
    of the pair).
    """

    def __init__(self, message: str, *, kind: str = "parse") -> None:
        super().__init__(message)
        self.kind = kind


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
    model: str | None = None,
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
    from durin.memory.llm_invoke import LLMResponse as _LLMResponse

    last_error: Exception | None = None
    kind = "parse"
    attempt_prompt = prompt
    attempts = 0
    for attempt in range(max_retries + 1):
        attempts = attempt + 1
        try:
            response = llm_invoke(attempt_prompt, model=model)
        except Exception as exc:  # noqa: BLE001
            # The provider already applied its own retry policy; repeating
            # the call here would only multiply a dead key or an outage.
            last_error, kind = exc, "provider"
            logger.warning(
                "absorb_judge LLM call failed (attempt %d/%d): %s",
                attempts, max_retries + 1, exc,
            )
            break
        if getattr(response, "finish_reason", "stop") == "error":
            # The provider reports failure as a response whose text is its
            # error message; parsing it would look like format drift.
            text = response.text if isinstance(response, _LLMResponse) else str(response)
            last_error, kind = JudgeError(f"provider error: {text[:200]}", kind="provider"), "provider"
            logger.warning(
                "absorb_judge provider error (attempt %d/%d): %s",
                attempts, max_retries + 1, text[:200],
            )
            break
        raw = response.text if isinstance(response, _LLMResponse) else str(response)
        try:
            return _parse_response(raw)
        except JudgeError as exc:
            last_error = exc
            logger.warning(
                "absorb_judge parse failed (attempt %d/%d): %s",
                attempts, max_retries + 1, exc,
            )
            # Tell the model what could not be parsed instead of re-sending
            # the same prompt blind: most parse failures are format drift
            # (prose around the envelope, a float confidence, a missing
            # closing marker), which the model corrects when told. Appended
            # at call time, so the template fingerprint — the judge
            # identity the verdict cache keys on — is unchanged.
            attempt_prompt = prompt + _RETRY_FEEDBACK.format(error=exc)

    raise JudgeError(
        f"absorb_judge failed after {attempts} attempt(s): {last_error}", kind=kind,
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
    inside a ``` block. We grab the largest fenced block to avoid
    accidentally formatting docs prose.
    """
    text = _TEMPLATE_PATH.read_text(encoding="utf-8")
    matches = re.findall(r"```(?:[a-z]*)\n(.*?)\n```", text, re.DOTALL)
    if not matches:
        raise JudgeError(
            f"absorb_judge template at {_TEMPLATE_PATH} has no fenced block"
        )
    return max(matches, key=len)


def _render_page_block(page: "EntityPage", *, mtime: datetime | None) -> str:
    """Render the WHOLE page for the judge — the canonical serialization, not a
    curated field subset. A new entity field is visible by default (the safe
    direction); only the free-text body is size-capped."""
    created = (
        mtime.astimezone(timezone.utc).isoformat()
        if mtime is not None
        else "(unknown)"
    )
    md = page.to_markdown()
    if len(md) > _PAGE_BUDGET_CHARS:
        md = md[:_PAGE_BUDGET_CHARS] + "\n…(truncated)"
    return f"- File last modified: {created}\n\n{md}"


# ---------------------------------------------------------------------------
# Output parsing
# ---------------------------------------------------------------------------


def _parse_response(raw: str) -> JudgeResult:
    """Extract verdict / confidence / reasoning from the markdown markers.

    Raises :class:`JudgeError` if any block is missing or malformed.
    Tolerates: extra prose around and between the blocks, case variation,
    emphasis or code fences around a value, a confidence written as a
    percentage or as a fraction of one, and a missing ``===END===`` after
    the reasoning.
    """
    if not raw or not isinstance(raw, str):
        raise JudgeError("empty or non-string LLM response")
    blocks = _blocks(raw)

    verdict_block = blocks.get("===VERDICT===")
    if verdict_block is None:
        raise JudgeError("missing ===VERDICT=== block")
    # The verdict is the block's first line reduced to its letters: emphasis
    # and a trailing period are tolerated, prose is not — "not the same,
    # different" or an echoed "same | different | unclear" must fail rather
    # than read as ``same`` and merge two entities.
    first_line = next((ln for ln in verdict_block.splitlines() if ln.strip()), "")
    verdict = re.sub(r"[^a-z]", "", first_line.lower())
    if verdict not in _VALID_VERDICTS:
        raise JudgeError(
            f"invalid verdict {first_line.strip()!r}; expected one of {sorted(_VALID_VERDICTS)}"
        )

    confidence_block = blocks.get("===CONFIDENCE===")
    if confidence_block is None:
        raise JudgeError("missing ===CONFIDENCE=== block")
    number = _RE_NUMBER.search(confidence_block)
    if number is None:
        raise JudgeError("non-integer confidence: no number in the block")
    value = float(number.group(1))
    if "." in number.group(1) and value <= 1.0:
        value *= 100.0
    confidence = int(round(value))
    if not 0 <= confidence <= 100:
        raise JudgeError(f"confidence {confidence} out of [0, 100]")

    reasoning_block = blocks.get("===REASONING===")
    if reasoning_block is None:
        raise JudgeError("missing ===REASONING=== / ===END=== block")
    reasoning = reasoning_block.strip().strip("`").strip()
    if not reasoning:
        raise JudgeError("empty reasoning block")

    return JudgeResult(
        verdict=verdict,
        confidence=confidence,
        reasoning=reasoning,
    )


_RETRY_FEEDBACK = (
    "\n\nYour previous reply could not be parsed ({error}). Reply again using "
    "exactly this envelope and nothing else:\n"
    "===VERDICT===\n<same|different|unclear>\n===CONFIDENCE===\n<integer 0-100>\n"
    "===REASONING===\n<your reasoning>\n===END===\n"
)


def judge_template_fingerprint() -> str:
    """Stable short hash of the judge prompt template.

    Verdict-cache key component: a template change means every cached verdict
    was produced by a different judge, so all pairs re-judge."""
    import hashlib

    return hashlib.sha256(_load_template().encode("utf-8")).hexdigest()[:12]
