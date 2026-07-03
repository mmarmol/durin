"""A rules-version bump pulls every previously-curated auto skill back into the
curation delta — the retro-repair trigger for the composition doctrine."""
from durin.agent import skills_store as ss

BODY = """---
name: some-skill
description: Do a thing. Use when the thing needs doing.
---
# Some Skill

Guidance.
"""


def test_v2_stamped_skill_needs_curation_again(tmp_path, monkeypatch):
    assert ss.dream_create_skill(tmp_path, "some-skill", BODY, "r").get("ok")
    # Stamp as curated under the CURRENT rules → stable, not re-reviewed.
    ss.mark_curated(tmp_path, "some-skill")
    assert ss.needs_curation(tmp_path, "some-skill") is False
    # The same stamp under yesterday's rules version → stale, re-enters the delta.
    monkeypatch.setattr(ss, "CURATION_RULES_VERSION", ss.CURATION_RULES_VERSION + 1)
    assert ss.needs_curation(tmp_path, "some-skill") is True
