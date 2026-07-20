"""Tests for the changelog reader (durin/changelog.py) and `durin changelog`."""

from __future__ import annotations

from typer.testing import CliRunner

from durin import changelog
from durin.cli.commands import app

runner = CliRunner()

SAMPLE = """# Changelog

Preamble that must not leak into any section body.

## 0.3.3 — 2026-07-19

### Highlights

- Thing A landed.

## 0.3.2 — 2026-07-10

- Thing B landed.
"""


def test_parse_splits_sections_newest_first():
    secs = changelog.parse(SAMPLE)
    assert [s.version for s in secs] == ["0.3.3", "0.3.2"]
    assert secs[0].heading == "## 0.3.3 — 2026-07-19"
    assert "Thing A landed." in secs[0].body
    assert "Thing B landed." in secs[1].body


def test_parse_keeps_subsections_and_drops_preamble():
    secs = changelog.parse(SAMPLE)
    assert "### Highlights" in secs[0].body
    assert all("Preamble that must not leak" not in s.body for s in secs)


def test_section_body_includes_its_heading():
    secs = changelog.parse(SAMPLE)
    assert secs[0].body.startswith("## 0.3.3 — 2026-07-19")


def test_find_hit_and_miss():
    secs = changelog.parse(SAMPLE)
    assert changelog.find(secs, "0.3.2").version == "0.3.2"
    assert changelog.find(secs, "9.9.9") is None


def test_versions_lists_newest_first():
    assert changelog.versions(changelog.parse(SAMPLE)) == ["0.3.3", "0.3.2"]


def test_current_exact_match(monkeypatch):
    monkeypatch.setattr("durin.__version__", "0.3.2")
    sec, fell_back = changelog.current(changelog.parse(SAMPLE))
    assert sec.version == "0.3.2"
    assert fell_back is False


def test_current_falls_back_to_newest(monkeypatch):
    monkeypatch.setattr("durin.__version__", "9.9.9")
    sec, fell_back = changelog.current(changelog.parse(SAMPLE))
    assert sec.version == "0.3.3"
    assert fell_back is True


def test_locate_finds_repo_changelog():
    text = changelog._locate()
    assert text is not None
    assert "# Changelog" in text


def test_pyproject_force_includes_changelog():
    """The wheel must ship CHANGELOG.md as package data, or a running agent
    can't consult it. Guard the force-include mapping so a pyproject refactor
    can't silently drop it (the release CI is what actually builds the wheel)."""
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]  # tests/cli/ -> repo root
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    force_include = data["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert force_include["CHANGELOG.md"] == "durin/CHANGELOG.md"


def _newest_version() -> str:
    return changelog.versions(changelog.parse(changelog._locate()))[0]


def test_cli_default_prints_a_version_section():
    result = runner.invoke(app, ["changelog"])
    assert result.exit_code == 0
    assert "## " in result.stdout


def test_cli_all_shows_multiple_versions():
    result = runner.invoke(app, ["changelog", "--all"])
    assert result.exit_code == 0
    assert result.stdout.count("## ") >= 2


def test_cli_specific_known_version():
    version = _newest_version()
    result = runner.invoke(app, ["changelog", version])
    assert result.exit_code == 0
    assert version in result.stdout


def test_cli_unknown_version_exits_nonzero():
    result = runner.invoke(app, ["changelog", "99.99.99"])
    assert result.exit_code == 1
