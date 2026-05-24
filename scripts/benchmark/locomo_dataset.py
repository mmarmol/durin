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
# nombradas. El orden refleja el §4.1 del paper.
_CATEGORY_BY_CODE = {
    1: "single_hop",
    2: "multi_hop",
    3: "temporal",
    4: "open_domain",
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
            conv = _parse_conversation(conv_idx, sample.get("conversation") or {})
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
            if not question or answer is None:
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


def _parse_conversation(idx: int, raw: dict[str, Any]) -> Conversation:
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
