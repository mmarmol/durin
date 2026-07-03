"""The skill composition doctrine, single-sourced from the `skill-creator` builtin.

Every surface that authors or curates skills (the dream skill-extract pass, the
daily curation judge, the in-session agent) must apply the same rule for what a
skill may contain: deterministic work belongs in a bundled script, orchestration
belongs in a workflow the skill delegates to, and only knowledge or runtime
judgment stays as prose. That rule is written once — in the builtin
`skill-creator` skill's "Before building" section — and this module loads it at
runtime so the prompts embed the canonical text instead of a paraphrase that
would drift.

`workflow_catalog_text` renders the workspace's workflow definitions as a
compact block for the same prompts, so an authoring pass can see what already
exists and delegate to it instead of re-describing it in prose.

`judge_composition` is the enforcement half: prompts are guidance, this is the
invariant. At skill-creation time it asks a judge model whether the body is a
prose-only narration of a workflow-shaped procedure; the store rejects that
body with the judge's reason so the author retries with feedback. The judge is
injectable (unit tests run without a provider) and failure-open: no judge, or
a judge error, accepts — the gate must never block authoring on infrastructure.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

# The exact heading in durin/skills/skill-creator/SKILL.md that opens the
# composition doctrine. Tests pin this string on purpose: renaming the section
# in the skill must break the build until this constant (and its consumers)
# are updated together.
DOCTRINE_HEADING = "## Before building: is a skill even the right tool?"


def composition_doctrine() -> str:
    """The doctrine section of the builtin `skill-creator` SKILL.md, verbatim.

    Returns the text from `DOCTRINE_HEADING` up to (not including) the next
    `## ` heading. Returns "" when the builtin or the section is missing —
    callers embed nothing rather than a stale copy.
    """
    from durin.agent.skills import BUILTIN_SKILLS_DIR

    md = Path(BUILTIN_SKILLS_DIR) / "skill-creator" / "SKILL.md"
    try:
        text = md.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("composition doctrine unavailable (%s): %s", md, exc)
        return ""
    start = text.find(DOCTRINE_HEADING)
    if start < 0:
        logger.warning("composition doctrine heading not found in %s", md)
        return ""
    end = text.find("\n## ", start + len(DOCTRINE_HEADING))
    section = text[start:] if end < 0 else text[start:end]
    return section.strip()


def workflow_catalog_text(workspace: str | Path) -> str:
    """One line per local workflow definition: name, description, I/O contract.

    Same source as the `list_workflows` tool (the JSON definitions under
    ``<workspace>/workflows/``); malformed definitions are skipped. Always
    returns a non-empty block so prompts can embed it unconditionally.
    """
    from durin.workflow.loader import load_workflow, workflows_dir

    d = workflows_dir(Path(workspace).expanduser())
    lines: list[str] = []
    if d.is_dir():
        for f in sorted(d.glob("*.json")):
            try:
                wf = load_workflow(workspace, f.stem)
            except Exception:  # noqa: BLE001 - a malformed definition is not this caller's problem
                continue
            io_bits = []
            for label, spec in (("input", wf.input), ("output", wf.output)):
                desc = (spec or {}).get("description") if isinstance(spec, dict) else None
                if desc:
                    io_bits.append(f"{label}: {desc}")
            io = f" ({'; '.join(io_bits)})" if io_bits else ""
            lines.append(f"- {wf.name} — {wf.description or 'no description'}{io}")
    if not lines:
        return "(no workflows installed)"
    return "\n".join(lines)


_COMPOSITION_GATE_PROMPT = """You review ONE skill body against durin's \
composition doctrine before it is saved. The doctrine, verbatim:

{doctrine}

Workflows available in this workspace:

{catalog}

The ONLY failure you may flag: the body is a PROSE-ONLY NARRATION of a \
workflow-shaped procedure — it walks the reader through multi-source fan-out \
(run several searches / process many items), gathering, and synthesis or a \
verification loop as manual steps, without delegating any of it to a workflow \
(via run_workflow) and without bundling a script. That narration belongs in a \
workflow; the skill should keep only the domain layer and delegate.

Err toward ACCEPTING. Accept bodies that are knowledge, conventions, judgment \
guidance, or decision procedures; accept bodies that delegate to a workflow or \
invoke bundled scripts; accept anything ambiguous.

Skill body to review:

---
{body}
---

Reply with a one-line reason, then end with EXACTLY one label on its own final \
line:
COMPLIANT — the body respects the doctrine (or is ambiguous).
NARRATION — the body manually narrates a workflow-shaped procedure. Name which \
steps should delegate (and to which workflow, if one above fits).
"""


def judge_composition(
    body: str,
    workspace: str | Path,
    judge: Callable[[str], str] | None,
) -> tuple[bool, str]:
    """Judge one skill body against the doctrine. Returns ``(ok, reason)``.

    Failure-open by design: a missing judge, a judge exception, or an
    unparseable reply all accept — enforcement must never turn an
    infrastructure problem into a lost skill.
    """
    if judge is None:
        return True, ""
    prompt = _COMPOSITION_GATE_PROMPT.format(
        doctrine=composition_doctrine() or "(doctrine unavailable)",
        catalog=workflow_catalog_text(workspace),
        body=body,
    )
    try:
        raw = str(judge(prompt) or "")
    except Exception as exc:  # noqa: BLE001 - failure-open: never block authoring on infra
        logger.warning("composition gate judge failed; accepting: %s", exc)
        return True, ""
    lines = [ln.strip() for ln in raw.strip().splitlines() if ln.strip()]
    if not lines:
        return True, ""
    last = lines[-1]
    if last.upper().startswith("NARRATION"):
        reason = last[len("NARRATION"):].strip(" —:-") or "narrates a workflow-shaped procedure"
        return False, reason
    if last.upper().startswith("COMPLIANT"):
        return True, ""
    logger.warning("composition gate: unparseable judge reply; accepting (head: %r)", raw[:120])
    return True, ""
