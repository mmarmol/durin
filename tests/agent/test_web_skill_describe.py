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


def test_describe_clawhub_fetches_skill_md(monkeypatch):
    # clawhub previews must show the real SKILL.md body, fetched from the
    # registry's raw-file endpoint — not degrade to an empty inline summary.
    raw_md = (
        "---\n"
        "name: Git\n"
        "description: Version control discipline\n"
        "---\n"
        "## When to Use\n\nGit work.\n"
    )
    seen = {}
    def _fake_get(url):
        seen["url"] = url
        return raw_md.encode()
    monkeypatch.setattr(si, "_http_get_bytes", _fake_get)
    status, payload = ss.web_skill_describe("clawhub:git")
    assert status == 200
    assert seen["url"].endswith("/api/v1/skills/git/file?path=SKILL.md")
    assert payload["description"] == "Version control discipline"
    assert "## When to Use" in payload["body"]


def test_describe_clawhub_degrades_on_fetch_error(monkeypatch):
    def _boom(url):
        raise RuntimeError("network down")
    monkeypatch.setattr(si, "_http_get_bytes", _boom)
    status, payload = ss.web_skill_describe("clawhub:git")
    assert status == 200
    assert payload["body"] == ""
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


def test_describe_returns_body_and_full_description(monkeypatch):
    long_desc = "X" * 400  # longer than the old 280-char clip
    raw_md = (
        "---\n"
        "name: test\n"
        f"description: {long_desc}\n"
        "---\n"
        "## How it works\n\nDoes the thing.\n"
    )
    _resolve_https(monkeypatch)
    monkeypatch.setattr(si, "_http_get_bytes", lambda url: raw_md.encode())
    status, payload = ss.web_skill_describe("https://example.com/SKILL.md")
    assert status == 200
    assert payload["description"] == long_desc            # not truncated to 280
    assert "## How it works" in payload["body"]           # markdown body returned
    assert "Does the thing." in payload["body"]


def test_describe_empty_string_returns_empty_body(monkeypatch):
    status, payload = ss.web_skill_describe("")
    assert status == 200
    assert payload["body"] == ""
