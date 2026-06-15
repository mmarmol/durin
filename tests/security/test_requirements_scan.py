# tests/security/test_requirements_scan.py
from pathlib import Path

from durin.security.requirements_scan import extract_requirements, resolve_display


def _write_skill(tmp_path: Path, name: str, frontmatter: str, body: str = "Hello.") -> Path:
    d = tmp_path / name
    d.mkdir()
    (d / "SKILL.md").write_text(f"---\n{frontmatter}\n---\n{body}\n")
    return d


def test_step1_declared_platforms_and_requires(tmp_path):
    d = _write_skill(tmp_path, "gh-skill", """
name: gh-skill
platforms: [macos, linux]
metadata:
  durin:
    requires:
      bins: [gh, jq]
      env: [GITHUB_TOKEN]
""")
    req = extract_requirements(d)
    assert req["platforms"]["value"] == ["macos", "linux"]
    assert req["platforms"]["inferred"] is False
    bin_names = [b["name"] for b in req["bins"]]
    assert "gh" in bin_names and "jq" in bin_names
    assert all(b["origin"] == "declared" for b in req["bins"])
    assert req["env"][0]["name"] == "GITHUB_TOKEN"
    assert req["env"][0]["origin"] == "declared"
    assert req["compatibility"] == ""


def test_step1_no_frontmatter_returns_empty_manifest(tmp_path):
    d = _write_skill(tmp_path, "bare", "name: bare", "Just text.")
    req = extract_requirements(d)
    assert req["platforms"]["value"] == []
    assert req["bins"] == []
    assert req["env"] == []


def test_step1_compatibility_field_preserved(tmp_path):
    d = _write_skill(tmp_path, "c", """
name: c
compatibility: "Requires macOS 12+ and homebrew."
""")
    req = extract_requirements(d)
    assert req["compatibility"] == "Requires macOS 12+ and homebrew."


def test_empty_skill_dir_does_not_crash(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    req = extract_requirements(d)
    assert req["bins"] == []


def test_step2_shebang_extracts_tool(tmp_path):
    d = _write_skill(tmp_path, "s", "name: s")
    sdir = d / "scripts"
    sdir.mkdir()
    (sdir / "run.sh").write_text("#!/usr/bin/env ffmpeg\nffmpeg -i input.mp4\n")
    req = extract_requirements(d)
    bin_names = [b["name"] for b in req["bins"]]
    assert "ffmpeg" in bin_names
    assert all(b["origin"] == "heuristic:script" for b in req["bins"] if b["name"] == "ffmpeg")


def test_step3_subprocess_invocation_extracts_tool(tmp_path):
    d = _write_skill(tmp_path, "s", "name: s")
    sdir = d / "scripts"
    sdir.mkdir()
    (sdir / "deploy.py").write_text(
        "import subprocess\n"
        'subprocess.run(["gh", "repo", "create"])\n'
    )
    req = extract_requirements(d)
    bin_names = [b["name"] for b in req["bins"]]
    assert "gh" in bin_names
    assert any(b["origin"] == "heuristic:script" for b in req["bins"] if b["name"] == "gh")


def test_declared_wins_over_heuristic(tmp_path):
    d = _write_skill(tmp_path, "s", """
name: s
metadata:
  durin:
    requires:
      bins: [gh]
""")
    sdir = d / "scripts"
    sdir.mkdir()
    (sdir / "x.py").write_text("import subprocess\nsubprocess.run(['gh', 'pr', 'list'])\n")
    req = extract_requirements(d)
    gh = [b for b in req["bins"] if b["name"] == "gh"]
    assert len(gh) == 1
    assert gh[0]["origin"] == "declared"


def test_step4_action_context_extracts_catalog_tool(tmp_path):
    d = _write_skill(tmp_path, "s", "name: s",
                     body="To list repos, run `gh` then `rg` for search.")
    req = extract_requirements(d, workspace=tmp_path)
    bin_names = [b["name"] for b in req["bins"]]
    assert "gh" in bin_names
    assert "rg" in bin_names
    assert all(b["origin"] == "heuristic:body" for b in req["bins"])


def test_step4_non_action_context_drops_tool(tmp_path):
    d = _write_skill(tmp_path, "s", "name: s",
                     body="This is similar to `gh` in some ways.")
    req = extract_requirements(d, workspace=tmp_path)
    bin_names = [b["name"] for b in req["bins"]]
    assert "gh" not in bin_names


def test_step4_unknown_tool_dropped(tmp_path):
    d = _write_skill(tmp_path, "s", "name: s",
                     body="To run things, use `totally-unknown-tool`.")
    req = extract_requirements(d, workspace=tmp_path)
    bin_names = [b["name"] for b in req["bins"]]
    assert "totally-unknown-tool" not in bin_names


def test_step4_in_code_block_dropped(tmp_path):
    d = _write_skill(tmp_path, "s", "name: s",
                     body="Example:\n```\nrun `gh`\n```\n")
    req = extract_requirements(d, workspace=tmp_path)
    bin_names = [b["name"] for b in req["bins"]]
    assert "gh" not in bin_names


def test_step5_infer_platform_from_brew_spec(tmp_path):
    d = _write_skill(tmp_path, "s", """
name: s
metadata:
  durin:
    install:
      - kind: brew
        formula: gh
""")
    req = extract_requirements(d)
    assert "macos" in req["platforms"]["value"]
    assert req["platforms"]["inferred"] is True


def test_step5_declared_platforms_win_over_inferred(tmp_path):
    d = _write_skill(tmp_path, "s", """
name: s
platforms: [linux]
metadata:
  durin:
    install:
      - kind: brew
        formula: gh
""")
    req = extract_requirements(d)
    assert req["platforms"]["value"] == ["linux"]
    assert req["platforms"]["inferred"] is False


def test_merge_llm_tools(tmp_path):
    d = _write_skill(tmp_path, "s", "name: s")
    req = extract_requirements(d, llm_tools=["gh", "ffmpeg"])
    bin_names = [b["name"] for b in req["bins"]]
    assert "gh" in bin_names and "ffmpeg" in bin_names
    assert all(b["origin"] == "llm" for b in req["bins"])


def test_heuristic_wins_over_llm(tmp_path):
    d = _write_skill(tmp_path, "s", "name: s",
                     body="To list repos, run `gh`.")
    req = extract_requirements(d, workspace=tmp_path, llm_tools=["gh"])
    gh = [b for b in req["bins"] if b["name"] == "gh"]
    assert len(gh) == 1
    assert gh[0]["origin"] == "heuristic:body"


def test_step5_conflict_warning_noted(tmp_path):
    d = _write_skill(tmp_path, "s", """
name: s
platforms: [linux]
metadata:
  durin:
    install:
      - kind: brew
        formula: gh
""")
    req = extract_requirements(d)
    assert "linux" in req["platforms"]["value"]
    assert req.get("platform_conflict") is True


def test_resolve_display_strips_origins(tmp_path):
    manifest = {
        "platforms": {"value": ["macos"], "inferred": False},
        "bins": [
            {"name": "gh", "origin": "declared", "available": None},
            {"name": "ffmpeg", "origin": "heuristic:body", "available": None},
        ],
        "env": [{"name": "TOKEN", "origin": "declared", "available": None}],
        "compatibility": "Needs brew.",
        "installable": False,
        "blocked_by_platform": False,
        "platform_conflict": False,
    }
    display = resolve_display(manifest, platform="macos", catalog={"ffmpeg": {"primary": {"kind": "brew", "value": "ffmpeg"}}})
    assert all("origin" not in b for b in display["bins"])
    assert "origin" not in display
    assert display["platform_ok"] is True
    assert display["bins"][0]["available"] in (True, False)


def test_resolve_display_platform_mismatch(tmp_path):
    manifest = {
        "platforms": {"value": ["linux"], "inferred": False},
        "bins": [],
        "env": [],
        "compatibility": "",
        "installable": False,
        "blocked_by_platform": False,
        "platform_conflict": False,
    }
    display = resolve_display(manifest, platform="macos", catalog={})
    assert display["platform_ok"] is False


def test_resolve_display_installable_computed(tmp_path):
    manifest = {
        "platforms": {"value": [], "inferred": False},
        "bins": [{"name": "gh", "origin": "declared", "available": None}],
        "env": [],
        "compatibility": "",
        "installable": False,
        "blocked_by_platform": False,
        "platform_conflict": False,
    }
    display = resolve_display(manifest, platform="macos", catalog={"gh": {"primary": {"kind": "brew", "value": "gh"}}})
    gh = [b for b in display["bins"] if b["name"] == "gh"][0]
    assert gh["installable"] is True
    assert gh["install_spec"] == "brew: gh"
