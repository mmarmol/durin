"""ModesService — list the registered agent modes for the composer picker.

Trivial read service: delegates to ``durin.agent.agent_mode.list_modes()`` and
projects each mode to the fields the UI needs. The mode list is data, so the
picker renders whatever is registered (built-ins plus any custom modes) without
hardcoding names or icons.

Escape hatch: ``ModesResult.modes`` is ``list[dict[str, Any]]`` — each mode is a
small open dict (``name``, ``description``, ``icon``, ``builtin``); custom modes
later add access detail to the same dicts without changing the wire schema.
"""

from __future__ import annotations

from typing import Any

from durin.service.principal import Principal, Scope
from durin.service.registry import route
from durin.service.types import Query, Result


class ModesListQuery(Query):
    """No inputs — lists every registered agent mode."""


class ModesResult(Result):
    modes: list[dict[str, Any]]  # escape hatch — open per-mode dict


class ModesService:
    """Return the registered agent modes (built-ins first)."""

    @route(
        "GET",
        "/api/v1/modes",
        scope=Scope.SYSTEM_READ.value,
        request_model=ModesListQuery,
        response_model=ModesResult,
        summary="List registered agent modes (build/plan/explore plus custom)",
    )
    async def list(
        self, query: ModesListQuery, principal: Principal
    ) -> ModesResult:
        principal.require(Scope.SYSTEM_READ)
        from durin.agent.agent_mode import list_modes

        modes = [
            {
                "name": m.name,
                "description": m.description,
                "icon": m.icon,
                "builtin": m.builtin,
            }
            for m in list_modes()
        ]
        return ModesResult(modes=modes)
