"""Live end-to-end checks against the real ClawHub registry.

Opt-in (network): run with ``DURIN_LIVE_REGISTRY_TESTS=1``. These prove the two
things unit mocks cannot — that the search adapter targets the *ranked* `/search`
endpoint (not the recency `/skills` list that silently ignores the query) and
that a clawhub preview fetches a real SKILL.md body. Both adapter methods degrade
to empty on any error, so when offline these self-skip rather than fail.
"""
import asyncio
import os

import pytest

from durin.agent.skill_registry import ClawHubRegistry
from durin.agent.skills_store import web_skill_describe

pytestmark = pytest.mark.network


def _require_live():
    if not os.environ.get("DURIN_LIVE_REGISTRY_TESTS"):
        pytest.skip("set DURIN_LIVE_REGISTRY_TESTS=1 to run live registry tests")


def test_live_clawhub_search_ranks_query_relevant_hits():
    _require_live()
    hits = asyncio.run(ClawHubRegistry().search("git", limit=10))
    if not hits:
        pytest.skip("clawhub returned no hits (offline or API change)")
    # The ranked /search endpoint returns git-relevant slugs; the old
    # /skills?search= list returned recency junk regardless of the query.
    slugs = [h.ref.removeprefix("clawhub:") for h in hits]
    assert all(h.ref.startswith("clawhub:") for h in hits)
    assert any("git" in s for s in slugs), slugs


def test_live_cross_source_merge_interleaves_sources():
    _require_live()
    from durin.agent.skill_registry import SkillsShRegistry, search_registries

    adapters = [SkillsShRegistry(), ClawHubRegistry()]
    hits = asyncio.run(search_registries("git", adapters=adapters, allowlist=[], limit=10))
    regs = [h.registry for h in hits]
    if "clawhub" not in regs or "skills.sh" not in regs:
        pytest.skip(f"need both sources live to test the merge; got {set(regs)}")
    # Rank-fair round-robin: clawhub's top hit surfaces near the top instead of
    # being buried beneath every skills.sh result (the 'orden entre fuentes' bug).
    assert regs.index("clawhub") <= 2, regs


def test_live_clawhub_describe_returns_skill_md_body():
    _require_live()
    status, payload = web_skill_describe("clawhub:git")
    assert status == 200
    if not payload["body"]:
        pytest.skip("clawhub describe returned no body (offline or API change)")
    # Real preview: a non-trivial SKILL.md body plus a frontmatter description.
    assert payload["description"]
    assert len(payload["body"]) > 50
