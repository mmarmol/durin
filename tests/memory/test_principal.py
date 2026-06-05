from datetime import datetime, timezone

from durin.memory.field_patch import FieldPatch
from durin.memory.memory_writer import write_entity
from durin.memory.principal import (
    ANONYMOUS,
    build_pinned_context,
    ensure_owner,
    list_always_on,
    mark_always_on,
    resolve_principal,
)

NOW = datetime(2026, 6, 5, tzinfo=timezone.utc)


def test_resolve_principal_channel_then_owner_then_anonymous():
    cmap = {"slack:U1": "person:alex"}
    assert resolve_principal("slack:U1", owner="person:marcelo", channel_map=cmap) == "person:alex"
    assert resolve_principal("slack:U9", owner="person:marcelo", channel_map=cmap) == "person:marcelo"
    assert resolve_principal(None) == ANONYMOUS


def test_ensure_owner_cold_start(tmp_path):
    created = ensure_owner(tmp_path, "person:marcelo", name="Marcelo")
    assert created is True
    assert (tmp_path / "memory/entities/person/marcelo.md").exists()
    assert ensure_owner(tmp_path, "person:marcelo") is False     # idempotent


def test_mark_and_list_always_on(tmp_path):
    write_entity(tmp_path, "practice:spanish",
                 [FieldPatch(kind="body_append", value="Respond in Spanish.",
                             author="agent", source_ref="s", at=NOW)],
                 create=True, name="Always Spanish")
    assert list_always_on(tmp_path) == []
    mark_always_on(tmp_path, "practice:spanish")
    assert "practice:spanish" in list_always_on(tmp_path)


def test_build_pinned_context_includes_principal_and_always_on(tmp_path):
    ensure_owner(tmp_path, "person:marcelo", name="Marcelo")
    write_entity(tmp_path, "person:marcelo",
                 [FieldPatch(kind="body_append", value="Architect; prefers Spanish.",
                             author="agent", source_ref="s", at=NOW)])
    write_entity(tmp_path, "practice:spanish",
                 [FieldPatch(kind="body_append", value="Always respond in Spanish.",
                             author="agent", source_ref="s", at=NOW)],
                 create=True, name="Always Spanish")
    mark_always_on(tmp_path, "practice:spanish")

    ctx = build_pinned_context(tmp_path, "person:marcelo")
    assert "Who you're talking to" in ctx
    assert "Marcelo" in ctx and "prefers Spanish" in ctx
    assert "Always-on guidance" in ctx
    assert "Always respond in Spanish." in ctx
    assert "<!--" not in ctx                       # provenance markers stripped
