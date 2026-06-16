"""Classify the not_found cases from recall_at_k.py.

For each fail where the expected substring didn't appear in top-100,
extract the agent's actual answer (`got`) from the v2 fail md file.
Manual eyeballing: is `got` semantically right (judge false-negative
penalizing different phrasing) or genuinely wrong (recall miss)?
"""
from __future__ import annotations

import json
import re
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
RECALL_OUT = _REPO / "bench-results/locomo/recall_at_k.json"
FAILS_DIR = _REPO / "bench-results/locomo/2026-05-25_074326_70f64867/failures"


def _parse_got(path: Path) -> str:
    text = path.read_text()
    g_match = re.search(r"^## Got\n(.+?)(?=\n## )", text, re.S | re.M)
    return g_match.group(1).strip() if g_match else ""


def main() -> None:
    records = json.loads(RECALL_OUT.read_text())
    not_found = [r for r in records if r["found_position"] is None]
    print(f"Found {len(not_found)} not_found cases\n")
    for i, r in enumerate(not_found, 1):
        qa_id = r["qa_id"]
        cat = r["category"]
        question = r["question"]
        expected = r["expected"]
        got = _parse_got(FAILS_DIR / f"{qa_id}.md")
        # Trim got to first 600 chars
        got_short = got[:600].replace("\n", " | ")
        print(f"--- [{i}/{len(not_found)}] {qa_id}  ({cat}) ---")
        print(f"Q: {question}")
        print(f"EXPECTED: {expected}")
        print(f"GOT: {got_short}")
        print()


if __name__ == "__main__":
    main()
