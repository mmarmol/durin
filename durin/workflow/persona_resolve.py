"""Shared persona resolver for both the agent loop and workflow node runner.

Given a persona name, looks it up in the config (user personas then built-ins),
loads the soul body from the SoulStore, and returns the model ref.
"""

from __future__ import annotations


def resolve_persona(config: object, name: str | None) -> tuple[str | None, str | None]:
    """Resolve a persona NAME to ``(soul_body, model_ref)``.

    Returns ``(None, None)`` when *name* is falsy, the persona is unknown, or
    any load step fails. Never raises — callers fall back to the default SOUL
    and default model.

    The ``if not name`` short-circuit is equivalent to the old loop path: the
    caller resolves the *name* (via ``resolve_active_persona_name``, which already
    applies ``agents.defaults.persona``), so a falsy name here means "no persona
    anywhere" — the same case where ``config.resolve_persona(None)`` returned None.
    ``config.workspace_path`` is the canonical SoulStore root for both callers.
    """
    if not name:
        return None, None
    try:
        persona = config.resolve_persona(name) if config is not None else None  # type: ignore[union-attr]
        if persona is None:
            return None, None
        from durin.souls.store import SoulStore
        body = SoulStore(config.workspace_path).read(persona.soul)  # type: ignore[union-attr]
        return (body or None), persona.model
    except Exception:  # noqa: BLE001 — best-effort; caller falls back gracefully
        return None, None
