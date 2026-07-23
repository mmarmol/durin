"""Tests for the secret store — durin/security/secrets.py."""

from __future__ import annotations

import json
import stat

import pytest

from durin.security.secrets import (
    SecretNotFoundError,
    SecretStore,
    is_secret_ref,
    is_valid_secret_name,
    make_ref,
    parse_secret_ref,
    scope_allows,
)

# -- reference grammar -------------------------------------------------------


def test_is_secret_ref_matches_whole_field_only() -> None:
    assert is_secret_ref("${secret:OPENAI_MAIN}")
    assert is_secret_ref("  ${secret:ATLASSIAN_WORK}  ")  # trimmed
    # Partial interpolation is NOT a reference — whole field only.
    assert not is_secret_ref("key=${secret:OPENAI_MAIN}")
    assert not is_secret_ref("${secret:bad-lowercase}")
    assert not is_secret_ref("sk-literal-key")
    assert not is_secret_ref(None)


def test_parse_and_make_ref_round_trip() -> None:
    assert parse_secret_ref(make_ref("OPENAI_MAIN")) == "OPENAI_MAIN"
    assert parse_secret_ref("not-a-ref") is None


def test_valid_secret_name() -> None:
    assert is_valid_secret_name("OPENAI_MAIN")
    assert is_valid_secret_name("A")
    assert not is_valid_secret_name("lower")
    assert not is_valid_secret_name("1LEADING_DIGIT")
    assert not is_valid_secret_name("HAS-DASH")
    assert not is_valid_secret_name("")


# -- scope -------------------------------------------------------------------


def test_scope_allows_exact_and_wildcard() -> None:
    assert scope_allows(["exec"], "exec")
    assert scope_allows(["skill:*"], "skill:deploy")
    assert scope_allows(["skill:deploy"], "skill:deploy")
    assert not scope_allows(["skill:deploy"], "skill:backup")
    assert not scope_allows(["provider:openai"], "exec")
    assert not scope_allows([], "exec")


# -- store round-trip --------------------------------------------------------


def test_store_put_load_round_trip(tmp_path) -> None:
    path = tmp_path / "secrets.json"
    store = SecretStore(path=path)
    store.put(
        "ATLASSIAN_WORK",
        value="tok-123",
        service="atlassian",
        account="work",
        description="work jira",
        scope=["exec", "skill:*"],
        origin="user",
    )

    reloaded = SecretStore(path=path).load()
    entry = reloaded.get("ATLASSIAN_WORK")
    assert entry is not None
    assert entry.value == "tok-123"
    assert entry.service == "atlassian"
    assert entry.account == "work"
    assert entry.scope == ["exec", "skill:*"]
    assert entry.created_at  # auto-stamped


def test_store_file_is_mode_600(tmp_path) -> None:
    path = tmp_path / "secrets.json"
    store = SecretStore(path=path)
    store.put("K", value="v", service="provider:openai")
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"


def test_store_exposes_no_public_save(tmp_path) -> None:
    """The store must not offer an unlocked raw write.

    ``save()`` used to be public and callable outside ``cross_process_lock`` —
    a caller could rewrite secrets.json from a stale in-memory snapshot and
    clobber a concurrent writer's changes (a proven lost-update hazard). The
    mutators (``put``, ``remove``, ``set_scope_locked``, ...) persist under
    the lock internally via the private ``_save``; there must be no public
    raw-write surface for callers to bypass that.
    """
    store = SecretStore(path=tmp_path / "secrets.json")
    assert not hasattr(store, "save")


def test_put_persists_without_a_separate_save_call(tmp_path) -> None:
    """put() alone is durable: a FRESH store reading from disk sees the entry.

    Proves the trailing ``store.save()`` calls removed from callers across the
    codebase were redundant — ``put()`` already persists inside the lock.
    """
    path = tmp_path / "secrets.json"
    SecretStore(path=path).put("K", value="v", service="x")
    assert SecretStore(path=path).load().get("K") is not None
    assert SecretStore(path=path).load().get("K").value == "v"


def test_remove_persists_without_a_separate_save_call(tmp_path) -> None:
    """remove() alone is durable: a FRESH store reading from disk sees the deletion."""
    path = tmp_path / "secrets.json"
    store = SecretStore(path=path)
    store.put("K", value="v", service="x")
    assert store.remove("K") is True
    assert SecretStore(path=path).load().get("K") is None


def test_store_rejects_invalid_name(tmp_path) -> None:
    store = SecretStore(path=tmp_path / "secrets.json")
    with pytest.raises(Exception, match="Invalid secret name"):
        store.put("bad-name", value="v", service="x")


def test_store_put_replace_keeps_created_at(tmp_path) -> None:
    store = SecretStore(path=tmp_path / "secrets.json")
    store.put("K", value="v1", service="atlassian")
    first = store.get("K").created_at
    store.put("K", value="v2", service="atlassian")
    assert store.get("K").value == "v2"
    assert store.get("K").created_at == first


def test_store_skips_malformed_entries_on_load(tmp_path) -> None:
    path = tmp_path / "secrets.json"
    path.write_text(
        json.dumps(
            {
                "_version": 1,
                "secrets": {
                    "GOOD": {"value": "v", "service": "atlassian"},
                    "bad-name": {"value": "v", "service": "x"},
                    "MISSING_VALUE": {"service": "x"},
                },
            }
        ),
        encoding="utf-8",
    )
    store = SecretStore(path=path).load()
    assert store.names() == ["GOOD"]


# -- resolution --------------------------------------------------------------


def test_resolve_passthrough_for_literals(tmp_path) -> None:
    store = SecretStore(path=tmp_path / "secrets.json")
    assert store.resolve("sk-literal") == "sk-literal"
    assert store.resolve(None) is None


def test_resolve_reference_returns_value(tmp_path) -> None:
    store = SecretStore(path=tmp_path / "secrets.json")
    store.put("OPENAI_MAIN", value="sk-real", service="provider:openai")
    assert store.resolve(make_ref("OPENAI_MAIN")) == "sk-real"


def test_resolve_missing_reference_raises(tmp_path) -> None:
    store = SecretStore(path=tmp_path / "secrets.json")
    with pytest.raises(SecretNotFoundError, match="GHOST"):
        store.resolve("${secret:GHOST}")


def test_resolve_ignores_scope(tmp_path) -> None:
    """A config reference is itself the grant — resolve never gates on scope."""
    store = SecretStore(path=tmp_path / "secrets.json")
    store.put("K", value="v", service="x", scope=[])  # empty scope
    assert store.resolve(make_ref("K")) == "v"


# -- collect_for (auto-injection) --------------------------------------------


def test_collect_for_filters_by_scope(tmp_path) -> None:
    store = SecretStore(path=tmp_path / "secrets.json")
    store.put("EXEC_TOKEN", value="e", service="atlassian", scope=["exec"])
    store.put("SKILL_TOKEN", value="s", service="x", scope=["skill:deploy"])
    store.put("PROVIDER_KEY", value="p", service="provider:openai",
              scope=["provider:openai"])

    exec_secrets = store.collect_for("exec")
    assert exec_secrets == {"EXEC_TOKEN": "e"}

    deploy_secrets = store.collect_for("skill:deploy")
    assert deploy_secrets == {"SKILL_TOKEN": "s"}


def test_find_by_service(tmp_path) -> None:
    store = SecretStore(path=tmp_path / "secrets.json")
    store.put("ATLASSIAN_WORK", value="w", service="atlassian", account="work")
    store.put("ATLASSIAN_HOME", value="h", service="atlassian", account="home")
    store.put("OPENAI", value="o", service="provider:openai")

    assert store.find_by_service("atlassian") == ["ATLASSIAN_HOME", "ATLASSIAN_WORK"]
    assert store.find_by_service("atlassian", account="work") == ["ATLASSIAN_WORK"]
    assert store.find_by_service("nonexistent") == []


def test_remove_and_set_scope(tmp_path) -> None:
    store = SecretStore(path=tmp_path / "secrets.json")
    store.put("K", value="v", service="x", scope=["exec"])
    assert store.set_scope("K", ["skill:*"]) is True
    assert store.get("K").scope == ["skill:*"]
    assert store.set_scope("UNKNOWN", ["exec"]) is False
    assert store.remove("K") is True
    assert store.remove("K") is False
