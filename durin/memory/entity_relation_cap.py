"""Per-entity relation cap enforcement (audit B-19, 2026-05-29).

`docs/architecture/memory/01_data_and_entities.md` §4.4 documented soft / hard
relation caps (50 / 200) as intent, but Dream apply never enforced
them. An entity could legitimately accumulate 500 relations with no
signal. B-19 ships the brake pedal:

- **Soft cap** (50): if a Dream apply takes the relation count
  ACROSS the threshold (was < 50, becomes ≥ 50), a telemetry event
  fires (`memory.entity_relation_cap_warned`) but the apply
  proceeds. Operators / dashboards can spot mega-hub formation
  before sub-paging (B-14) becomes necessary.

- **Hard cap** (200): if a Dream apply would take the relation
  count over the hard cap (was ≤ 200, would become > 200), the
  apply is rejected with `DreamApplyFailureKind.VALIDATION`. A
  telemetry event fires (`memory.entity_relation_cap_rejected`)
  with the entity_ref + the would-be count so operators can act.

The signal is intentionally lossy at the soft cap (warn only) and
hard at the hard cap (reject). This matches the doc 01 §4.4 spec
without adding architectural surface beyond what the cap requires.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

__all__ = [
    "HARD_RELATION_CAP",
    "RelationCapDecision",
    "SOFT_RELATION_CAP",
    "check_relation_cap",
]


SOFT_RELATION_CAP: int = 50
HARD_RELATION_CAP: int = 200


@dataclass(frozen=True)
class RelationCapDecision:
    """Outcome of one cap check.

    ``action``:
      - ``"ok"``: count stays below the soft cap.
      - ``"warn"``: this call crossed the soft cap; apply continues
        but the caller should emit the warn event.
      - ``"reject"``: this call would cross the hard cap; the apply
        must NOT proceed; the caller emits the reject event and
        returns ``DreamApplyFailureKind.VALIDATION``.

    ``new_count`` is the count the entity WOULD reach if the apply
    proceeded — useful for both telemetry payloads and downstream
    decisions.
    """

    entity_ref: str
    current_count: int
    new_count: int
    action: Literal["ok", "warn", "reject"]

    @property
    def cap_warned(self) -> bool:
        return self.action == "warn"

    @property
    def cap_rejected(self) -> bool:
        return self.action == "reject"


def check_relation_cap(
    *,
    entity_ref: str,
    current_count: int,
    adding: int,
) -> RelationCapDecision:
    """Decide whether a Dream apply that adds *adding* relations to
    an entity currently at *current_count* should be allowed, allowed-
    with-warning, or rejected.

    The cap is on the FINAL count, not the per-call delta — adding 5
    to an entity at 198 is rejected because the result (203) exceeds
    the hard cap, even though 5 alone is small.
    """
    new_count = max(0, int(current_count) + max(0, int(adding)))
    if new_count > HARD_RELATION_CAP:
        action: Literal["ok", "warn", "reject"] = "reject"
    elif current_count <= SOFT_RELATION_CAP < new_count:
        action = "warn"
    else:
        action = "ok"
    return RelationCapDecision(
        entity_ref=entity_ref,
        current_count=current_count,
        new_count=new_count,
        action=action,
    )
