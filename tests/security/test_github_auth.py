"""Precedence + safety of the shared GitHub token resolver."""

from durin.security.github_auth import (
    SHARED_SECRET_NAME,
    resolve_github_token,
    resolve_github_token_with_source,
)


def test_prefers_gh_cli_over_everything():
    tok = resolve_github_token(
        env={"GITHUB_TOKEN": "env"},
        gh_runner=lambda: "gh",
        secret_getter=lambda n: "sek",
    )
    assert tok == "gh"


def test_env_when_no_gh():
    assert (
        resolve_github_token(
            env={"GITHUB_TOKEN": "env"}, gh_runner=lambda: None, secret_getter=lambda n: None
        )
        == "env"
    )
    assert (
        resolve_github_token(
            env={"DURIN_GITHUB_TOKEN": "d"}, gh_runner=lambda: None, secret_getter=lambda n: None
        )
        == "d"
    )


def test_shared_secret_when_no_gh_or_env():
    got = resolve_github_token(
        env={},
        gh_runner=lambda: None,
        secret_getter=lambda n: "shared" if n == SHARED_SECRET_NAME else None,
    )
    assert got == "shared"


def test_legacy_secret_fallback_when_shared_absent():
    got = resolve_github_token(
        env={},
        gh_runner=lambda: None,
        secret_getter=lambda n: "legacy" if n == "OLD_SKILLS_TOKEN" else None,
        legacy_secret_names=["OLD_SKILLS_TOKEN"],
    )
    assert got == "legacy"


def test_shared_secret_beats_legacy():
    got = resolve_github_token(
        env={},
        gh_runner=lambda: None,
        secret_getter=lambda n: {"GITHUB_OAUTH": "shared", "OLD": "legacy"}.get(n),
        legacy_secret_names=["OLD"],
    )
    assert got == "shared"


def test_anonymous_when_nothing_configured():
    assert (
        resolve_github_token(env={}, gh_runner=lambda: None, secret_getter=lambda n: None) == ""
    )


def test_flaky_gh_runner_degrades_not_crashes():
    def boom():
        raise OSError("gh exploded")

    assert (
        resolve_github_token(
            env={"GITHUB_TOKEN": "env"}, gh_runner=boom, secret_getter=lambda n: None
        )
        == "env"
    )


def test_unreadable_secret_store_degrades_to_anonymous():
    def boom(_name):
        raise RuntimeError("store locked")

    assert resolve_github_token(env={}, gh_runner=lambda: None, secret_getter=boom) == ""


# --- source reporting (so the UI can be honest about who owns the token) ------


def test_source_is_gh_when_gh_wins():
    _tok, src = resolve_github_token_with_source(
        env={"GITHUB_TOKEN": "e"}, gh_runner=lambda: "g", secret_getter=lambda n: "s"
    )
    assert src == "gh"


def test_source_is_env():
    tok, src = resolve_github_token_with_source(
        env={"GITHUB_TOKEN": "e"}, gh_runner=lambda: None, secret_getter=lambda n: None
    )
    assert (tok, src) == ("e", "env")


def test_source_is_secret_for_shared_and_legacy():
    tok, src = resolve_github_token_with_source(
        env={}, gh_runner=lambda: None,
        secret_getter=lambda n: "s" if n == SHARED_SECRET_NAME else None,
    )
    assert (tok, src) == ("s", "secret")
    tok, src = resolve_github_token_with_source(
        env={}, gh_runner=lambda: None,
        secret_getter=lambda n: "L" if n == "OLD" else None, legacy_secret_names=["OLD"],
    )
    assert (tok, src) == ("L", "secret")


def test_source_is_empty_when_anonymous():
    assert resolve_github_token_with_source(
        env={}, gh_runner=lambda: None, secret_getter=lambda n: None
    ) == ("", "")
