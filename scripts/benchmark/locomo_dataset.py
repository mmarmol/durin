"""LoCoMo dataset loader + stratified subset selector.

LoCoMo (Long Conversation Memory, Maharana et al. 2024) ships ten
multi-session conversation pairs with ~150 QAs per pair, distributed
across five categories: single-hop, multi-hop, temporal, open-domain,
and adversarial.

Source: https://github.com/snap-research/locomo

This loader keeps the dataset local-only (we don't want a hard
runtime dep on a remote fetch). The user provides ``--data-path`` to
``locomo_run.py`` pointing at the downloaded JSON. First-time setup:

    curl -L -o ~/.cache/durin/locomo10.json \\
      https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json

If the path doesn't exist the loader raises a friendly error with
the curl command above.

The loader normalises the on-disk shape into a tidy ``QA`` dataclass
the harness consumes one at a time. Stratified sampling picks N/5
QAs per category for representative coverage (LoCoMo paper §4.1
defines five categories that exercise different memory mechanisms).
"""

from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

__all__ = [
    "QA",
    "Conversation",
    "LoCoMoDatasetError",
    "load_dataset",
    "stratified_subset",
]

logger = logging.getLogger(__name__)

# LoCoMo paper §4.1 — five categorías que ejercitan mecanismos distintos
# de memoria. El benchmark reporta accuracy global pero la lectura
# accionable está en cómo varía por categoría.
CATEGORIES = ("single_hop", "multi_hop", "temporal", "open_domain", "adversarial")

# Mapeo de los códigos numéricos del dataset (1..5) a las categorías
# nombradas. Canonical per mem0's published benchmark code
# (``mem0/memory-benchmarks/benchmarks/locomo/prompts.py::
# CATEGORY_NAMES``) — VERIFIED against the raw counts in
# ``locomo10.json`` which yield 282 / 321 / 96 / 841 / 446
# for codes 1..5. Audit H13 (2026-05-29) fixed a pre-existing
# swap that re-labelled the four non-adversarial categories
# (paper §4.1 narrative order ≠ dataset code-to-label order);
# every prior bench-X labelling that called single_hop "our
# weakest category" was really pointing at multi_hop.
_CATEGORY_BY_CODE = {
    1: "multi_hop",
    2: "temporal",
    3: "open_domain",
    4: "single_hop",
    5: "adversarial",
}


class LoCoMoDatasetError(RuntimeError):
    """Raised when the dataset is missing, malformed, or the requested
    sampling can't be satisfied."""


@dataclass(frozen=True)
class QA:
    """One question-answer pair with the full conversation context."""

    qa_id: str          # ``conv-{conv_idx}-q{idx}`` — stable across runs
    conv_id: str        # source conversation identifier
    category: str       # one of CATEGORIES
    question: str
    answer: str         # ground-truth answer string
    evidence: list[str] = field(default_factory=list)
    # ``conversation`` is attached so the harness can seed memory with the
    # transcript before asking the question. It's NOT serialized into
    # per-QA traces (would blow up disk) — only the conv_id is enough
    # to recover it from the dataset on replay.
    conversation: "Conversation | None" = None


@dataclass(frozen=True)
class Session:
    """One conversation session — a contiguous block of turns with a
    timestamp. LoCoMo sessions span weeks; the date is part of the
    temporal reasoning the agent must do."""

    index: int          # 1-based session number within the conversation
    date_time: str      # ISO-ish string from the dataset
    turns: list[dict[str, Any]]  # raw {speaker, text, [dia_id]} entries


@dataclass(frozen=True)
class Conversation:
    conv_id: str
    speaker_a: str
    speaker_b: str
    sessions: list[Session]
    # Sample-level fields from the LoCoMo JSON that sit OUTSIDE the
    # ``conversation`` block but carry answer-relevant info for some QAs
    # (verified in doc 28 §4.5). Empty dict / list when absent.
    event_summary: dict[str, Any] = field(default_factory=dict)
    observation: dict[str, Any] = field(default_factory=dict)
    session_summary: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# loader
# ---------------------------------------------------------------------------


def load_dataset(path: str | Path) -> list[QA]:
    """Read a LoCoMo JSON file and return a flat list of QA objects.

    The on-disk schema (per snap-research/locomo) is a list of
    conversation samples, each with ``conversation`` (sessions +
    metadata) and ``qa`` (list of question dicts with numeric category
    codes 1..5).

    Defensive: rows that lack a category code OR carry an unknown code
    are dropped with a warning rather than crashing — the dataset has
    historically been re-released with shape variations.
    """
    path = Path(path).expanduser()
    if not path.is_file():
        raise LoCoMoDatasetError(
            f"LoCoMo dataset not found at {path}.\n"
            "Download once with:\n"
            "  mkdir -p ~/.cache/durin && curl -L -o ~/.cache/durin/locomo10.json \\\n"
            "    https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json"
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LoCoMoDatasetError(f"failed to parse {path}: {exc}") from exc

    if not isinstance(raw, list):
        raise LoCoMoDatasetError(
            f"expected top-level list in {path}, got {type(raw).__name__}"
        )

    qas: list[QA] = []
    skipped = 0
    for conv_idx, sample in enumerate(raw):
        try:
            conv = _parse_conversation(
                conv_idx,
                sample.get("conversation") or {},
                event_summary=sample.get("event_summary") or {},
                observation=sample.get("observation") or {},
                session_summary=sample.get("session_summary") or {},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("skipping malformed conversation %s: %s", conv_idx, exc)
            continue
        for q_idx, q in enumerate(sample.get("qa") or []):
            if not isinstance(q, dict):
                skipped += 1
                continue
            cat_code = q.get("category")
            if not isinstance(cat_code, int):
                # Some releases ship "1"/"2"/...; tolerate that.
                try:
                    cat_code = int(cat_code)
                except (TypeError, ValueError):
                    skipped += 1
                    continue
            category = _CATEGORY_BY_CODE.get(cat_code)
            if category is None:
                skipped += 1
                continue
            question = q.get("question") or ""
            answer = q.get("answer")
            if not question:
                skipped += 1
                continue
            # Audit H14 (2026-05-29): LoCoMo adversarial questions
            # (category code 5) come with ``answer=None`` by design —
            # the agent is expected to REFUSE to answer because the
            # fact isn't in the conversation. Pre-H14 the loader
            # skipped them (444 / 446 of the adversarial set were
            # invisible) so every prior bench reported adversarial
            # over a sample of size 2 — mostly noise. The string
            # sentinel ``"__REFUSE__"`` flags these for the judge to
            # score with a refusal rubric instead of substring match.
            if answer is None:
                if category == "adversarial":
                    answer = "__REFUSE__"
                else:
                    skipped += 1
                    continue
            qas.append(QA(
                qa_id=f"{conv.conv_id}-q{q_idx}",
                conv_id=conv.conv_id,
                category=category,
                question=str(question).strip(),
                answer=str(answer).strip(),
                evidence=list(q.get("evidence") or []),
                conversation=conv,
            ))
    if skipped:
        logger.info("load_dataset: skipped %d malformed/uncategorised QAs", skipped)
    return qas


def _parse_conversation(
    idx: int,
    raw: dict[str, Any],
    *,
    event_summary: dict[str, Any] | None = None,
    observation: dict[str, Any] | None = None,
    session_summary: dict[str, Any] | None = None,
) -> Conversation:
    """Normalise the per-sample ``conversation`` block.

    Session keys in the on-disk JSON are ``session_1``, ``session_2``,
    … with a sibling ``session_<n>_date_time`` carrying the timestamp.
    We walk the keys deterministically by index so the seeding order
    matches the original temporal sequence.
    """
    speaker_a = raw.get("speaker_a") or "User A"
    speaker_b = raw.get("speaker_b") or "User B"
    sessions: list[Session] = []
    # Walk session indices in order. Some samples have gaps (rare), so
    # iterate up to the highest numbered key.
    session_indices: list[int] = []
    for key in raw.keys():
        if key.startswith("session_") and not key.endswith("_date_time"):
            try:
                session_indices.append(int(key.removeprefix("session_")))
            except ValueError:
                continue
    for n in sorted(session_indices):
        turns = raw.get(f"session_{n}")
        if not isinstance(turns, list):
            continue
        date_time = str(raw.get(f"session_{n}_date_time") or "")
        sessions.append(Session(index=n, date_time=date_time, turns=turns))
    conv_id = str(raw.get("conv_id") or f"conv-{idx}")
    return Conversation(
        conv_id=conv_id,
        speaker_a=str(speaker_a),
        speaker_b=str(speaker_b),
        sessions=sessions,
        event_summary=event_summary or {},
        observation=observation or {},
        session_summary=session_summary or {},
    )


# ---------------------------------------------------------------------------
# stratified subset
# ---------------------------------------------------------------------------


def stratified_subset(
    qas: list[QA],
    per_category: int,
    *,
    seed: int = 42,
    categories: Iterable[str] = CATEGORIES,
    allow_undersupplied: bool = False,
) -> list[QA]:
    """Pick ``per_category`` QAs from each category, deterministically.

    Returns a flat list ordered by category then by sample index so a
    re-run with the same seed produces the same subset (essential for
    reproducible comparisons across commits).

    When ``allow_undersupplied=False`` (default), raises
    :class:`LoCoMoDatasetError` if any category has fewer than
    ``per_category`` samples — fail loudly so reports aren't silently
    skewed toward over-represented categories.

    When ``allow_undersupplied=True``, takes ``min(per_category, len)``
    from each category and logs which ones were short. Useful for
    larger samples where ``adversarial`` (only 2 QAs in locomo10) would
    otherwise cap the whole run at 2/category.
    """
    rng = random.Random(seed)
    by_cat: dict[str, list[QA]] = {c: [] for c in categories}
    for qa in qas:
        if qa.category in by_cat:
            by_cat[qa.category].append(qa)

    out: list[QA] = []
    short: list[str] = []
    for cat in categories:
        bucket = by_cat[cat]
        take = min(per_category, len(bucket))
        if len(bucket) < per_category:
            short.append(f"{cat} has only {len(bucket)} (asked {per_category})")
            if not allow_undersupplied:
                continue
        if take == 0:
            continue
        sampled = sorted(rng.sample(bucket, take), key=lambda q: q.qa_id)
        out.extend(sampled)
    if short and not allow_undersupplied:
        raise LoCoMoDatasetError(
            "stratified_subset: under-supplied categories — "
            + "; ".join(short)
            + ". Use allow_undersupplied=True (CLI: --allow-undersupplied) "
              "to take min(per_category, available) instead."
        )
    return out


def proportional_subset(
    qas: list[QA],
    *,
    total_n: int,
    seed: int = 42,
    categories: Iterable[str] = CATEGORIES,
) -> list[QA]:
    """Pick ~``total_n`` QAs proportional to each category's share of
    the corpus (audit H19, 2026-05-29).

    LoCoMo's category distribution is highly skewed (single_hop 42%,
    adversarial 23%, multi_hop 14%, temporal 16%, open_domain 5%
    in the locomo10.json). ``stratified_subset`` with ``N`` per
    category over-represents rare categories and under-represents
    single_hop — a 25/25/25/25/25 sample lifts adversarial from its
    natural 23% share to 20% (close), but lifts open_domain from
    5% to 20% (4× over). A score on such a subset isn't directly
    comparable to systems that benchmark against the full corpus
    (mem0, Letta, MemMachine all do).

    Allocation rule:
      target[c] = max(1, round(total_n * count[c] / total_qas))
      adjusted to land exactly on total_n via a deterministic
      largest-fractional-remainder pass.

    ``max(1, ...)`` guarantees no category disappears at small N;
    the final adjustment pass uses largest-remainder so the sum
    always matches ``total_n`` exactly (no off-by-rounding).

    Reproducibility contract:
      - Same (qas, total_n, seed) → identical output across runs.
      - Sort is `(category, qa_id)` so a re-run that picks the same
        set returns it in the same order.
      - Per-category allocation is purely arithmetic (no rng), so
        only the WITHIN-category sampling is seed-driven.
    """
    if not qas or total_n <= 0:
        return []
    rng = random.Random(seed)
    cat_list = [c for c in categories]
    by_cat: dict[str, list[QA]] = {c: [] for c in cat_list}
    for qa in qas:
        if qa.category in by_cat:
            by_cat[qa.category].append(qa)

    # Filter to non-empty categories so a missing category doesn't
    # consume a slot from the `max(1, ...)` floor.
    present = [c for c in cat_list if by_cat[c]]
    if not present:
        return []
    counts = {c: len(by_cat[c]) for c in present}
    total = sum(counts.values())

    # Compute raw fractional targets then round via largest-remainder
    # so the sum lands exactly on total_n. Each present category
    # gets at least 1.
    raw = {c: total_n * counts[c] / total for c in present}
    floors = {c: max(1, int(raw[c])) for c in present}
    # Sum after floors; adjust against total_n
    overflow = sum(floors.values()) - total_n
    if overflow > 0:
        # Trim from the categories with the smallest fractional part
        # (largest-overshoot first) while respecting the ≥ 1 floor.
        remainders = sorted(
            present, key=lambda c: (raw[c] - int(raw[c])),
        )
        for c in remainders:
            if overflow <= 0:
                break
            if floors[c] > 1:
                floors[c] -= 1
                overflow -= 1
    elif overflow < 0:
        # Add to the categories with the largest fractional part
        remainders = sorted(
            present, key=lambda c: -(raw[c] - int(raw[c])),
        )
        i = 0
        while overflow < 0:
            c = remainders[i % len(remainders)]
            if floors[c] < counts[c]:
                floors[c] += 1
                overflow += 1
            i += 1
            if i > total_n * 4:  # paranoid runaway guard
                break

    out: list[QA] = []
    for cat in present:
        bucket = by_cat[cat]
        take = min(floors[cat], len(bucket))
        sampled = sorted(rng.sample(bucket, take), key=lambda q: q.qa_id)
        out.extend(sampled)
    # Final sort by (category, qa_id) for deterministic ordering
    out.sort(key=lambda q: (q.category, q.qa_id))
    return out
