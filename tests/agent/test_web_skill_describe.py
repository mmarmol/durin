import durin.agent.skills_import as si
import durin.agent.skills_store as ss
from durin.agent.skill_resolve import ResolveResult, SkillCandidate


def _resolve_https(monkeypatch):
    monkeypatch.setattr(
        "durin.agent.skill_resolve.resolve_candidates",
        lambda ref: ResolveResult([
            SkillCandidate("test", "https://example.com/SKILL.md", "https"),
        ]),
    )


def test_describe_returns_platforms_and_requires(monkeypatch):
    raw_md = (
        "---\n"
        "name: test\n"
        "description: A test skill\n"
        "platforms: [macos]\n"
        "metadata:\n"
        "  durin:\n"
        "    requires:\n"
        "      bins: [gh]\n"
        "      env: [TOKEN]\n"
        "---\nBody.\n"
    )
    _resolve_https(monkeypatch)
    monkeypatch.setattr(si, "_http_get_bytes", lambda url: raw_md.encode())
    status, payload = ss.web_skill_describe("https://example.com/SKILL.md")
    assert status == 200
    assert payload["description"] == "A test skill"
    assert payload["platforms"] == ["macos"]
    assert payload["requires"]["bins"] == ["gh"]
    assert payload["requires"]["env"] == ["TOKEN"]


def test_describe_missing_platforms_and_requires_are_none(monkeypatch):
    raw_md = (
        "---\n"
        "name: test\n"
        "description: Simple skill\n"
        "---\nBody.\n"
    )
    _resolve_https(monkeypatch)
    monkeypatch.setattr(si, "_http_get_bytes", lambda url: raw_md.encode())
    status, payload = ss.web_skill_describe("https://example.com/SKILL.md")
    assert status == 200
    assert payload["platforms"] is None
    assert payload["requires"] is None


def test_describe_empty_string_returns_none(monkeypatch):
    status, payload = ss.web_skill_describe("")
    assert status == 200
    assert payload["platforms"] is None
    assert payload["requires"] is None


def test_describe_clawhub_returns_none_platforms_requires():
    status, payload = ss.web_skill_describe("clawhub:some/slug")
    assert status == 200
    assert payload["platforms"] is None
    assert payload["requires"] is None


def test_describe_no_candidates_returns_none_platforms_requires(monkeypatch):
    monkeypatch.setattr(
        "durin.agent.skill_resolve.resolve_candidates",
        lambda ref: ResolveResult(unresolved_reason="nope"),
    )
    status, payload = ss.web_skill_describe("github:owner/repo/demo")
    assert status == 200
    assert payload["platforms"] is None
    assert payload["requires"] is None
