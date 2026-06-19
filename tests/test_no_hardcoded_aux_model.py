"""Anti-regression guard: out-of-loop LLM callers (memory dream, skill judge) must
resolve the user's preset, never a hardcoded model literal.

The requirement: every memory-dream and skill-judge call uses the purpose-specific
model when configured, else the user's default preset — NEVER the bundled
``glm-5.1``. This test fails if a bare ``"glm-5.1"`` literal creeps back into any
of the runtime modules that drive those calls (comments are allowed).
"""
from __future__ import annotations

import re
from pathlib import Path

RUNTIME_MODULES = [
    "durin/memory/llm_invoke.py",
    "durin/memory/dream_passes.py",
    "durin/memory/refine_dream.py",
    "durin/memory/absorb_judge.py",
    "durin/memory/always_on_dream.py",
    "durin/security/skill_judge.py",
    "durin/agent/skills_store.py",
    "durin/channels/websocket.py",
]

_LITERAL = re.compile(r"""["']glm-5\.1["']""")


def test_no_hardcoded_glm_5_1_in_runtime() -> None:
    root = Path(__file__).resolve().parents[1]
    offenders: list[str] = []
    for rel in RUNTIME_MODULES:
        for i, line in enumerate((root / rel).read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]  # ignore trailing comments
            if _LITERAL.search(code):
                offenders.append(f"{rel}:{i}: {line.strip()}")
    assert not offenders, "hardcoded glm-5.1 in runtime modules:\n" + "\n".join(offenders)
