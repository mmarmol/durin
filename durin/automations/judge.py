"""LLM judge for a trigger's optional ``semantic`` condition: strict-JSON
verdict parsing.

Automations classify a run's own outcome deterministically from the
workflow's result (see ``durin.automations.classify``), so — unlike the
former loops runtime — there is no goal-judge prompt here, only the
trigger-filter judge ``durin.automations.matcher.TriggerMatcher`` calls
through its ``semantic_judge`` callable.
"""

from __future__ import annotations

import json
import re

FILTER_PROMPT = """You are deciding whether an incoming message matches a trigger condition.
Condition: {condition}
Message:
---
{summary}
---
Answer with ONLY a JSON object: {{"match": bool}}.
If it is not clearly a match, answer match=false."""


def build_filter_prompt(condition: str, summary: str) -> str:
    return FILTER_PROMPT.format(condition=condition, summary=summary[:6000])


def parse_filter_verdict(text: str) -> bool:
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        return False
    try:
        data = json.loads(m.group(0))
    except Exception:
        return False
    return bool(data.get("match"))
