"""A refusal to resume a run, with the short word that names why.

A refusal is the sentence a person or a model reads. The word (``kind``) is
what is counted: it is fixed where the sentence is made, so nothing ever works
out why from the sentence's wording. A refusal is still a ``str`` equal to its
sentence, so every caller that compares it or prints it is unaffected.
"""

from __future__ import annotations

# The words a refusal can carry; ``other`` for one that fits none of them.
KINDS = frozenset({
    "script_alive",    # something of a script the dead process started may still run
    "folder_pruned",   # the run's working folder or stored input is gone
    "step_gone",       # the step it was in is no longer in the workflow
    "superseded",      # a later run on the same key took the folder over
    "automation",      # an automation started it and never resumes it
    "mcp_tools",       # its steps need tools only the agent's resume has
    "child_running",   # a sub-workflow run of it is still recorded as running
    "lost_race",       # another caller acted on the run first or still is
    "other",
})


class Refusal(str):
    """A refusal's sentence, carrying ``kind``, one of ``KINDS``."""

    kind: str

    def __new__(cls, text: str, kind: str = "other") -> "Refusal":
        if kind not in KINDS:
            raise ValueError(f"unknown refusal kind {kind!r}")
        out = super().__new__(cls, text)
        out.kind = kind
        return out

    def __reduce__(self) -> tuple:
        # Copying and pickling rebuild it with both of its arguments.
        return (Refusal, (str(self), self.kind))


def kind_of(refusal: object) -> str:
    """The word of ``refusal``; ``other`` for text that carries none."""
    kind = getattr(refusal, "kind", None)
    return kind if kind in KINDS else "other"
