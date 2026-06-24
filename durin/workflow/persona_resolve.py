"""Shared persona resolver for both the agent loop and workflow node runner.

Given a persona name, looks it up in the config (user personas then built-ins),
loads the soul body from the SoulStore, and returns the model ref.
"""

from __future__ import annotations


def resolve_persona(
    config: object, name: str | None, workspace: object = None
) -> tuple[str | None, str | None]:
    """Resolve a persona NAME to ``(soul_body, model_ref)``.

    Returns ``(None, None)`` when *name* is falsy, the persona is unknown, or
    any load step fails. Never raises — callers fall back to the default SOUL
    and default model.

    The ``if not name`` short-circuit is equivalent to the old loop path: the
    caller resolves the *name* (via ``resolve_active_persona_name``, which already
    applies ``agents.defaults.persona``), so a falsy name here means "no persona
    anywhere" — the same case where ``config.resolve_persona(None)`` returned None.

    *workspace* is the SoulStore root — the loop passes its own ``self.workspace``
    and the node runner its session workspace (the original loop read souls from
    ``self.workspace``, which can differ from ``config.workspace_path`` in tests).
    Falls back to ``config.workspace_path`` when not given.
    """
    if not name:
        return None, None
    try:
        persona = config.resolve_persona(name) if config is not None else None  # type: ignore[union-attr]
        if persona is None:
            return None, None
        from durin.souls.store import SoulStore
        root = workspace if workspace is not None else config.workspace_path  # type: ignore[union-attr]
        body = SoulStore(root).read(persona.soul)
        return (body or None), persona.model
    except Exception:  # noqa: BLE001 — best-effort; caller falls back gracefully
        return None, None
