"""LLM judge for assertion checks: strict-JSON verdict parsing."""

from __future__ import annotations

import json
import re

PROMPT = """You are verifying whether a goal was reached, based only on the evidence.
Goal intent: {intent}
Assertions to check (answer each true/false):
{assertions}
Evidence (final output of the work):
---
{evidence}
---
Answer with ONLY a JSON object: {{"intent_met": bool, "assertions": {{"<assertion text>": bool, ...}}}}.
If the evidence does not clearly show the goal was met, answer intent_met=false."""


def build_prompt(intent: str, assertions: list[str], evidence: str) -> str:
    lines = "\n".join(f"- {a}" for a in assertions) or "(none)"
    return PROMPT.format(intent=intent, assertions=lines, evidence=evidence[:6000])


def parse_verdict(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        return {"intent_met": False, "assertions": {}}
    try:
        data = json.loads(m.group(0))
    except Exception:
        return {"intent_met": False, "assertions": {}}
    return {"intent_met": bool(data.get("intent_met")),
            "assertions": {str(k): bool(v) for k, v in (data.get("assertions") or {}).items()}}
