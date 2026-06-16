"""Tests for the `durin auth token` CLI group."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from durin.cli.commands import app
from durin.security.api_tokens import ApiTokenStore
from durin.service.auth import AuthService
from durin.service.principal import Scope
from durin.service.types import ForbiddenError

runner = CliRunner()


@pytest.fixture()
def tokens_path(tmp_path):
    """Redirect ApiTokenStore to a temp file for test isolation."""
    with patch("durin.config.paths.get_data_dir", return_value=tmp_path):
        yield tmp_path / "api_tokens.json"


# ---------------------------------------------------------------------------
# issue
# ---------------------------------------------------------------------------


def test_issue_prints_plaintext_and_id(tokens_path) -> None:
    result = runner.invoke(
        app,
        ["auth", "token", "issue", "--scopes", "secrets:read", "--label", "ci-bot"],
    )
    assert result.exit_code == 0, result.output
    assert "nbwt_" in result.output
    assert "store this now" in result.output
    assert "ci-bot" in result.output
    # token_id must appear (it is an 8-hex-char string); validate by checking the store
    store = ApiTokenStore(path=tokens_path)
    tokens = store.list_tokens()
    assert len(tokens) == 1
    assert tokens[0]["label"] == "ci-bot"
    assert "secrets:read" in tokens[0]["scopes"]
    # Plaintext / hash must not be in list_tokens() output
    assert "hash" not in str(tokens)
    assert "nbwt_" not in str(tokens)


def test_issue_rejects_unknown_scope(tokens_path) -> None:
    result = runner.invoke(
        app,
        ["auth", "token", "issue", "--scopes", "secrets:read,not-a-real-scope"],
    )
    assert result.exit_code == 1
    assert "Unknown scope" in result.output
    assert "not-a-real-scope" in result.output


def test_issue_rejects_empty_scopes(tokens_path) -> None:
    result = runner.invoke(
        app,
        ["auth", "token", "issue", "--scopes", "   "],
    )
    assert result.exit_code == 1
    assert "empty" in result.output.lower() or "Unknown" in result.output


def test_issue_non_expiring_by_default(tokens_path) -> None:
    result = runner.invoke(
        app,
        ["auth", "token", "issue", "--scopes", "admin"],
    )
    assert result.exit_code == 0, result.output
    assert "non-expiring" in result.output
    store = ApiTokenStore(path=tokens_path)
    assert store.list_tokens()[0]["expires_at"] is None


def test_issue_with_ttl(tokens_path) -> None:
    result = runner.invoke(
        app,
        ["auth", "token", "issue", "--scopes", "settings:read", "--ttl", "3600"],
    )
    assert result.exit_code == 0, result.output
    assert "3600s" in result.output
    store = ApiTokenStore(path=tokens_path)
    assert store.list_tokens()[0]["expires_at"] is not None


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_list_shows_issued_token_no_plaintext(tokens_path) -> None:
    store = ApiTokenStore(path=tokens_path)
    store.issue(["secrets:read"], label="mybot")

    result = runner.invoke(app, ["auth", "token", "list"])
    assert result.exit_code == 0, result.output
    assert "mybot" in result.output
    assert "secrets:read" in result.output
    # Plaintext and hash must never appear in list output
    assert "nbwt_" not in result.output
    assert "hash" not in result.output


def test_list_empty_shows_message(tokens_path) -> None:
    result = runner.invoke(app, ["auth", "token", "list"])
    assert result.exit_code == 0, result.output
    assert "No tokens" in result.output


# ---------------------------------------------------------------------------
# revoke
# ---------------------------------------------------------------------------


def test_revoke_removes_token(tokens_path) -> None:
    store = ApiTokenStore(path=tokens_path)
    token_id, _ = store.issue(["admin"])

    result = runner.invoke(app, ["auth", "token", "revoke", token_id])
    assert result.exit_code == 0, result.output
    assert "revoked" in result.output
    assert store.list_tokens() == []


def test_revoke_unknown_errors(tokens_path) -> None:
    result = runner.invoke(app, ["auth", "token", "revoke", "deadbeef"])
    assert result.exit_code == 1
    assert "No token" in result.output


# ---------------------------------------------------------------------------
# E2E scope enforcement: issue secrets:read-only token, resolve via AuthService
# ---------------------------------------------------------------------------


def test_e2e_scope_enforcement(tokens_path) -> None:
    """Issue a secrets:read-only token; assert it passes secrets:read but
    raises ForbiddenError on secrets:write."""
    store = ApiTokenStore(path=tokens_path)
    svc = AuthService(store=store)
    token_id, plaintext = store.issue(["secrets:read"], label="readonly")

    principal = svc.resolve(plaintext)
    assert principal is not None
    assert principal.kind == "remote"
    assert principal.subject == token_id

    # Must pass secrets:read
    principal.require(Scope.SECRETS_READ)  # no exception

    # Must raise on secrets:write
    with pytest.raises(ForbiddenError):
        principal.require(Scope.SECRETS_WRITE)
