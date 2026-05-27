"""Recall@k measurement on v2 LoCoMo fails.

For each failed QA in the v2 bench, seed a fresh workspace from the
LoCoMo dataset, build the vector index, run memory_search with
top_k=100, and report:
- whether the expected answer substring appears in any returned row
- if yes, at what position (#1..#100) and inferred source_type
- if not, classify as recall miss

The goal is to distinguish "ranking problem" (expected in top-100 but
not top-10) from "recall problem" (expected not even in top-100,
embeddings/seeding insufficient). The next-step plan depends on which
dominates.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

# Add repo root for local imports.
_REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO))

from scripts.benchmark.locomo_dataset import load_dataset  # noqa: E402
from scripts.benchmark.locomo_harness import (  # noqa: E402
    _build_vector_index,
    _seed_memory_from_conversation,
)


def _infer_source_type(headline: str) -> str:
    """Inference from headline patterns produced by the harness seeds."""
    if not headline:
        return "unknown"
    if headline.startswith("event["):
        return "event_summary"
    if headline.startswith("observation["):
        return "observation"
    if headline.startswith("Session summary"):
        return "session_summary"
    return "turn_quote"


def _parse_fail_file(path: Path) -> dict[str, Any]:
    """Extract qa_id, conv_id, category, question, expected from failure md."""
    text = path.read_text()
    qa_id = path.stem  # e.g. "conv-2-q57"
    m = re.match(r"^conv-(\d+)-q\d+$", qa_id)
    conv_idx = int(m.group(1)) if m else -1

    # Headings: "# qa_id — category — FAIL (...)"
    cat_match = re.search(r"^# \S+ — (\w+) —", text, re.MULTILINE)
    category = cat_match.group(1) if cat_match else "?"

    q_match = re.search(r"^## Question\n(.+?)(?=\n## )", text, re.S | re.M)
    e_match = re.search(r"^## Expected\n(.+?)(?=\n## )", text, re.S | re.M)
    question = q_match.group(1).strip() if q_match else ""
    expected = e_match.group(1).strip() if e_match else ""
    return {
        "qa_id": qa_id,
        "conv_idx": conv_idx,
        "category": category,
        "question": question,
        "expected": expected,
    }


def _expected_substrings(expected: str) -> list[str]:
    """Split expected answer into candidate substrings for matching.

    Many expected answers are short tokens ("Sweden"). Others are
    phrasal ("Strength and motivation") — split on comma + the word
    "and" to get individual tokens.
    """
    parts = re.split(r",|\s+and\s+", expected, flags=re.IGNORECASE)
    out = []
    for p in parts:
        p = p.strip().strip(".").strip()
        # Keep tokens that are 3+ chars; very short tokens cause false
        # positive substring matches ("at", "a", "to").
        if len(p) >= 3:
            out.append(p)
    return out or [expected.strip()]


def _find_in_rows(expected: str, rows: list[dict], workspace: Path) -> dict[str, Any]:
    """Search rows top-down for any expected-substring match in haystack.

    Returns dict with `position` (1-indexed, None if not found),
    `source_type` of the matching row, and the matching needle.
    """
    from durin.memory.storage import load_entry
    needles = [n.lower() for n in _expected_substrings(expected)]
    for i, row in enumerate(rows):
        # warm-tier vector returns headline/summary, NOT body. We must
        # load the body from disk to do a proper recall check.
        haystack_parts = [
            str(row.get("summary") or ""),
            str(row.get("headline") or ""),
        ]
        # Try to enrich with the body from disk.
        uri = str(row.get("id") or "")
        # uri shape for vector entries: "<entry_id>" or "memory/<class>/<id>"
        # The id in vector is just the entry_id, class_name is separate.
        class_name = row.get("class_name", "")
        if class_name and class_name != "entity_page":
            md_path = workspace / "memory" / class_name / f"{uri}.md"
            if md_path.is_file():
                try:
                    entry = load_entry(md_path)
                    if entry.body:
                        haystack_parts.append(entry.body)
                except Exception:
                    pass
        haystack = " ".join(haystack_parts).lower()
        for needle in needles:
            if needle in haystack:
                return {
                    "position": i + 1,
                    "source_type": _infer_source_type(str(row.get("headline") or "")),
                    "matched_needle": needle,
                }
    return {"position": None, "source_type": "", "matched_needle": ""}


def _seed_one_workspace(conv, workspace: Path) -> None:
    """Seed + build vector index for one conversation."""
    _seed_memory_from_conversation(workspace, conv)
    _build_vector_index(workspace)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--fails-dir",
        type=Path,
        default=_REPO / "bench-results/locomo/2026-05-25_074326_70f64867/failures",
        help="Directory with v2 fail md files.",
    )
    parser.add_argument(
        "--data-path",
        type=Path,
        default=Path("~/.cache/durin/locomo10.json").expanduser(),
    )
    parser.add_argument("--top-k", type=int, default=100)
    parser.add_argument(
        "--limit", type=int, default=0,
        help="Limit number of fails processed (0 = all)."
    )
    parser.add_argument(
        "--out", type=Path, default=_REPO / "bench-results/locomo/recall_at_k.json",
    )
    args = parser.parse_args()

    # Load fails
    fail_files = sorted(args.fails_dir.glob("*.md"))
    fails = [_parse_fail_file(f) for f in fail_files]
    if args.limit > 0:
        fails = fails[: args.limit]
    print(f"Loaded {len(fails)} v2 fails from {args.fails_dir.name}")

    # Load dataset, index by conv_idx (the bench harness assigns these
    # sequentially as conv-0, conv-1, ...)
    all_qas = load_dataset(args.data_path)
    convs_by_idx: dict[int, Any] = {}
    for qa in all_qas:
        if qa.conversation is None:
            continue
        m = re.match(r"^conv-(\d+)$", qa.conv_id)
        if m:
            convs_by_idx[int(m.group(1))] = qa.conversation
    print(f"Indexed {len(convs_by_idx)} unique conversations")

    # Group fails by conv to seed once per conv
    fails_by_conv: dict[int, list[dict]] = defaultdict(list)
    for fail in fails:
        fails_by_conv[fail["conv_idx"]].append(fail)

    # Load embedding provider once for reuse
    from durin.memory.embedding import FastembedProvider
    from durin.memory.vector_index import VectorIndex, vector_index_available
    if not vector_index_available():
        sys.exit("LanceDB unavailable")
    from durin.config.loader import load_config
    cfg = load_config()
    provider = FastembedProvider(model=cfg.memory.embedding.model)

    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="recall_at_k_") as tmpd:
        tmp_root = Path(tmpd)
        for conv_idx in sorted(fails_by_conv.keys()):
            conv = convs_by_idx.get(conv_idx)
            if conv is None:
                print(f"  conv-{conv_idx}: NOT FOUND in dataset, skipping")
                continue
            workspace = tmp_root / f"conv-{conv_idx}"
            workspace.mkdir(parents=True, exist_ok=True)
            print(f"  conv-{conv_idx}: seeding + building index "
                  f"({len(fails_by_conv[conv_idx])} fails)...")
            _seed_one_workspace(conv, workspace)
            vi = VectorIndex(workspace, provider)
            for fail in fails_by_conv[conv_idx]:
                rows = vi.search(fail["question"], top_k=args.top_k)
                hit = _find_in_rows(fail["expected"], rows, workspace)
                rec = {
                    **fail,
                    "top_k": args.top_k,
                    "total_rows": len(rows),
                    "found_position": hit["position"],
                    "found_source_type": hit["source_type"],
                    "matched_needle": hit["matched_needle"],
                    "top_1_headline": (
                        str(rows[0].get("headline", "")) if rows else ""
                    ),
                    "top_1_source_type": (
                        _infer_source_type(str(rows[0].get("headline", "")))
                        if rows else ""
                    ),
                }
                results.append(rec)

    # Summary
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(results, indent=2, ensure_ascii=False))
    print(f"\nWrote {len(results)} records to {args.out}")

    # Histogram
    buckets = {
        "top_1": 0,
        "top_2_5": 0,
        "top_6_10": 0,
        "top_11_30": 0,
        "top_31_100": 0,
        "not_found": 0,
    }
    by_source_at_top1 = defaultdict(int)
    by_source_at_match = defaultdict(int)
    for r in results:
        pos = r["found_position"]
        if pos is None:
            buckets["not_found"] += 1
        elif pos == 1:
            buckets["top_1"] += 1
        elif pos <= 5:
            buckets["top_2_5"] += 1
        elif pos <= 10:
            buckets["top_6_10"] += 1
        elif pos <= 30:
            buckets["top_11_30"] += 1
        else:
            buckets["top_31_100"] += 1
        by_source_at_top1[r["top_1_source_type"]] += 1
        if pos is not None:
            by_source_at_match[r["found_source_type"]] += 1

    print("\n=== Position of expected substring in top-100 ===")
    for k, v in buckets.items():
        print(f"  {k:14s}: {v}")
    print("\n=== Source_type of TOP-1 returned (currently ranked first) ===")
    for k, v in sorted(by_source_at_top1.items(), key=lambda kv: -kv[1]):
        print(f"  {k:18s}: {v}")
    print("\n=== Source_type of MATCHING row (where expected was found) ===")
    for k, v in sorted(by_source_at_match.items(), key=lambda kv: -kv[1]):
        print(f"  {k:18s}: {v}")


if __name__ == "__main__":
    main()
