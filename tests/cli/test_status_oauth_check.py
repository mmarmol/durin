"""Regression tests for `durin status` OAuth token reporting.

Previously the command printed `✓ (OAuth)` for any provider marked
``is_oauth=True`` in the registry — completely ignoring whether the
user had actually logged in. Users (rightly) found that confusing
when they saw codex / copilot marked as configured without ever
having run a login.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from durin.cli.commands import app

runner = CliRunner()


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run status against a fresh HOME so we don't depend on the developer's tokens."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: home)
    # The autouse conftest fixture pins DURIN_HOME to an unrelated tmp; these
    # tests plant tokens/config under ``home/.durin``, so point durin_home()
    # there too (it reads $DURIN_HOME with priority over Path.home()).
    monkeypatch.setenv("DURIN_HOME", str(home / ".durin"))
    return home


def test_status_does_not_claim_codex_logged_in_without_token(isolated_home: Path) -> None:
    """Empty $HOME → no OAuth provider should report ✓."""
    # Plant a config so status has something to load (otherwise it
    # short-circuits before printing per-provider rows).
    cfg_dir = isolated_home / ".durin"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text("{}", encoding="utf-8")

    with patch("durin.config.loader.get_config_path", return_value=cfg_dir / "config.json"):
        result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    # status only lists CONFIGURED providers — with no token, codex /
    # copilot must NOT appear in the Providers line.
    assert "OpenAI Codex" not in result.output
    assert "Github Copilot" not in result.output


def test_status_reports_codex_present_when_token_file_exists(isolated_home: Path) -> None:
    """A token file under the legacy `~/.durin/oauth/` path counts as logged in."""
    cfg_dir = isolated_home / ".durin"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text("{}", encoding="utf-8")

    oauth_dir = cfg_dir / "oauth"
    oauth_dir.mkdir()
    (oauth_dir / "openai_codex.json").write_text('{"access_token": "fake"}', encoding="utf-8")

    with patch("durin.config.loader.get_config_path", return_value=cfg_dir / "config.json"):
        result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    # Configured codex appears in the Providers line, tagged OAuth.
    assert "OpenAI Codex (OAuth)" in result.output
    # Copilot has no token → absent.
    assert "Github Copilot" not in result.output


def test_any_token_present_returns_false_without_files(isolated_home: Path) -> None:
    """Unit test for the shared helper — no files, no match."""
    from durin.utils.oauth import any_token_present

    assert any_token_present("openai_codex") is False
    assert any_token_present("github_copilot") is False


def test_any_token_present_finds_legacy_path(isolated_home: Path) -> None:
    """Legacy `~/.durin/oauth/<name>.json` is treated as a logged-in marker."""
    from durin.utils.oauth import any_token_present

    oauth_dir = isolated_home / ".durin" / "oauth"
    oauth_dir.mkdir(parents=True)
    (oauth_dir / "openai_codex.json").write_text("{}", encoding="utf-8")

    assert any_token_present("openai_codex") is True
    assert any_token_present("github_copilot") is False


def test_any_token_present_finds_alt_legacy_path(isolated_home: Path) -> None:
    """Alt legacy path `~/.<provider>/auth.json` also counts."""
    from durin.utils.oauth import any_token_present

    alt = isolated_home / ".github_copilot"
    alt.mkdir()
    (alt / "auth.json").write_text("{}", encoding="utf-8")

    assert any_token_present("github_copilot") is True


def test_token_storage_paths_includes_kit_paths_when_available() -> None:
    """When `oauth-cli-kit` is importable we should consult its layout too."""
    from durin.utils.oauth import token_storage_paths

    paths = token_storage_paths("openai_codex")
    # We can't assert exact path contents without depending on a
    # specific kit version, but the function should always return at
    # least the legacy candidates.
    assert len(paths) >= 2
    assert all(isinstance(p, Path) for p in paths)
