"""P6 — runnable_install_specs: safe specs become commands; dangerous/download dropped."""
from pathlib import Path

from durin.agent.skills_import import runnable_install_specs


def _skill(tmp_path: Path, frontmatter: str) -> Path:
    d = tmp_path / "s"
    d.mkdir()
    (d / "SKILL.md").write_text(f"---\n{frontmatter}\n---\nbody\n", encoding="utf-8")
    return d


def test_brew_and_npm_become_commands(tmp_path):
    fm = (
        "name: s\n"
        "metadata:\n"
        "  tools:\n"
        "    install:\n"
        "      - {kind: brew, formula: gh}\n"
        "      - {kind: npm, package: prettier}\n"
    )
    out = runnable_install_specs(_skill(tmp_path, fm))
    cmds = [s["command"] for s in out]
    assert "brew install gh" in cmds
    assert "npm install -g prettier" in cmds
    assert all(s["needs_privileges"] is False for s in out)  # brew/npm = user-level


def test_apt_flagged_needs_privileges(tmp_path):
    fm = (
        "name: s\n"
        "metadata:\n"
        "  tools:\n"
        "    install:\n"
        "      - {kind: apt, package: ripgrep}\n"
    )
    out = runnable_install_specs(_skill(tmp_path, fm))
    assert out[0]["command"] == "apt-get install -y ripgrep"
    assert out[0]["needs_privileges"] is True


def test_download_kind_excluded(tmp_path):
    fm = (
        "name: s\n"
        "metadata:\n"
        "  tools:\n"
        "    install:\n"
        "      - {kind: download, url: 'https://example.com/x.sh'}\n"
    )
    out = runnable_install_specs(_skill(tmp_path, fm))
    assert out == []


def test_dangerous_spec_dropped(tmp_path):
    # an unsafe npm spec (shell metachars) is flagged dangerous by validate_install_specs
    fm = (
        "name: s\n"
        "metadata:\n"
        "  tools:\n"
        "    install:\n"
        "      - {kind: npm, package: 'evil && rm -rf /'}\n"
    )
    out = runnable_install_specs(_skill(tmp_path, fm))
    assert out == []


def test_no_specs_returns_empty(tmp_path):
    out = runnable_install_specs(_skill(tmp_path, "name: s\n"))
    assert out == []
