from datetime import datetime, timezone
from pathlib import Path

from durin.memory.entity_page import EntityPage
from durin.memory.relation_hygiene import (
    canonicalize_page_relations,
    run_consolidate_relations_pass,
)

NOW = datetime(2026, 6, 5, tzinfo=timezone.utc)


def _legacy_page_with_hyphen_relation(tmp_path: Path) -> Path:
    """Write a page whose on-disk relation type carries a legacy hyphen form
    (bypassing the write-time normalizer, to simulate pre-existing data)."""
    md = tmp_path / "memory" / "entities" / "topic" / "dogs.md"
    md.parent.mkdir(parents=True, exist_ok=True)
    page = EntityPage(
        type="topic", name="Dogs",
        relations=[{"to": "topic:cysts", "type": "occurs-in"}],
    )
    page.save(md)
    return md


def test_canonicalize_page_rewrites_hyphen_form():
    page = EntityPage(type="topic", name="Dogs",
                      relations=[{"to": "topic:cysts", "type": "occurs-in"}])
    changed, dropped = canonicalize_page_relations(page)
    assert changed is True and dropped == 0
    assert page.relations[0]["type"] == "occurs_in"


def test_canonicalize_drops_duplicate_exposed_by_rename():
    page = EntityPage(type="topic", name="Dogs", relations=[
        {"to": "topic:cysts", "type": "occurs-in"},
        {"to": "topic:cysts", "type": "occurs_in"},  # same edge, different spelling
    ])
    changed, dropped = canonicalize_page_relations(page)
    assert changed is True and dropped == 1
    assert len(page.relations) == 1


def test_pass_reports_and_fixes_existing(tmp_path: Path):
    md = _legacy_page_with_hyphen_relation(tmp_path)
    r = run_consolidate_relations_pass(tmp_path)
    assert r["types_before"] == 1 and r["types_after"] == 1
    assert r["pages_changed"] == 1
    # the on-disk page now carries the canonical form
    assert EntityPage.from_file(md).relations[0]["type"] == "occurs_in"
    # idempotent — a second run rewrites nothing
    assert run_consolidate_relations_pass(tmp_path)["pages_changed"] == 0
