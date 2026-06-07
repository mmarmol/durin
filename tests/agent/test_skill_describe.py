import durin.agent.skills_import as si
import durin.agent.skills_store as ss
from durin.agent.skill_resolve import ResolveResult, SkillCandidate


def _resolve_github(monkeypatch):
    """Stub resolve_candidates → one normalized github candidate (the GitHub tree
    API resolution is exercised by skill_resolve's own tests)."""
    monkeypatch.setattr(
        "durin.agent.skill_resolve.resolve_candidates",
        lambda ref: ResolveResult([SkillCandidate("demo", "github:owner/repo@main/demo", "github")]),
    )


def test_describe_github_parses_frontmatter(monkeypatch):
    _resolve_github(monkeypatch)
    md = b"---\nname: demo\ndescription: Scrape and crawl websites.\n---\nbody\n"
    monkeypatch.setattr(si, "_http_get_bytes", lambda url: md)
    status, payload = ss.web_skill_describe("github:owner/repo/demo")
    assert status == 200
    assert payload["description"] == "Scrape and crawl websites."


def test_describe_network_error_is_empty(monkeypatch):
    _resolve_github(monkeypatch)

    def boom(url):
        raise RuntimeError("boom")

    monkeypatch.setattr(si, "_http_get_bytes", boom)
    status, payload = ss.web_skill_describe("github:owner/repo/demo")
    assert status == 200
    assert payload["description"] == ""


def test_describe_unresolvable_is_empty(monkeypatch):
    monkeypatch.setattr(
        "durin.agent.skill_resolve.resolve_candidates",
        lambda ref: ResolveResult(unresolved_reason="nope"),
    )
    status, payload = ss.web_skill_describe("github:owner/repo/demo")
    assert status == 200
    assert payload["description"] == ""


def test_describe_clawhub_is_empty():
    status, payload = ss.web_skill_describe("clawhub:some/slug")
    assert status == 200
    assert payload["description"] == ""
