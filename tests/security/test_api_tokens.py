"""Tests for ApiTokenStore (durin/security/api_tokens.py)."""

from __future__ import annotations

import json
import time

import pytest

from durin.security.api_tokens import ApiTokenStore, _hash_token


@pytest.fixture()
def store(tmp_path):
    return ApiTokenStore(path=tmp_path / "api_tokens.json")


# ---------------------------------------------------------------------------
# issue
# ---------------------------------------------------------------------------


def test_issue_returns_plaintext(store):
    token_id, plaintext = store.issue(["secrets:read"])
    assert plaintext.startswith("nbwt_")
    assert token_id  # non-empty id


def test_issue_stores_hash_not_plaintext(store, tmp_path):
    token_id, plaintext = store.issue(["secrets:read"])
    raw = json.loads((tmp_path / "api_tokens.json").read_text())
    entry = raw["tokens"][token_id]
    assert plaintext not in json.dumps(raw), "plaintext must not appear in JSON"
    assert "hash" in entry
    assert "salt" in entry
    # Verify the stored hash is correct.
    assert entry["hash"] == _hash_token(entry["salt"], plaintext)


def test_issue_stores_scopes_and_label(store):
    token_id, _ = store.issue(["settings:read", "cron:read"], label="ci-bot")
    tokens = {t["token_id"]: t for t in store.list_tokens()}
    assert tokens[token_id]["label"] == "ci-bot"
    assert set(tokens[token_id]["scopes"]) == {"settings:read", "cron:read"}


def test_issue_with_ttl_sets_expires_at(store):
    before = time.time()
    token_id, _ = store.issue(["admin"], ttl_s=3600)
    tokens = {t["token_id"]: t for t in store.list_tokens()}
    expires_at = tokens[token_id]["expires_at"]
    assert expires_at is not None
    assert before + 3600 <= expires_at <= before + 3601


def test_issue_without_ttl_expires_at_is_none(store):
    token_id, _ = store.issue(["admin"])
    tokens = {t["token_id"]: t for t in store.list_tokens()}
    assert tokens[token_id]["expires_at"] is None


# ---------------------------------------------------------------------------
# resolve
# ---------------------------------------------------------------------------


def test_resolve_valid_token_returns_entry(store):
    token_id, plaintext = store.issue(["secrets:read"])
    entry = store.resolve(plaintext)
    assert entry is not None
    assert entry["token_id"] == token_id
    assert "secrets:read" in entry["scopes"]


def test_resolve_wrong_token_returns_none(store):
    store.issue(["admin"])
    assert store.resolve("nbwt_wrongtoken") is None


def test_resolve_expired_token_returns_none(store):
    token_id, plaintext = store.issue(["admin"], ttl_s=-1)  # already expired
    assert store.resolve(plaintext) is None


def test_resolve_bumps_last_used_at(store):
    token_id, plaintext = store.issue(["admin"])
    assert store.list_tokens()[0]["last_used_at"] is None
    store.resolve(plaintext)
    assert store.list_tokens()[0]["last_used_at"] is not None


# ---------------------------------------------------------------------------
# revoke
# ---------------------------------------------------------------------------


def test_revoke_existing_token(store):
    token_id, plaintext = store.issue(["admin"])
    assert store.revoke(token_id) is True
    assert store.resolve(plaintext) is None


def test_revoke_nonexistent_returns_false(store):
    assert store.revoke("deadbeef") is False


# ---------------------------------------------------------------------------
# list_tokens
# ---------------------------------------------------------------------------


def test_list_never_leaks_hash_or_plaintext(store):
    token_id, plaintext = store.issue(["admin"], label="lbl")
    tokens = store.list_tokens()
    assert len(tokens) == 1
    t = tokens[0]
    blob = json.dumps(t)
    assert "hash" not in t
    assert "salt" not in t
    assert plaintext not in blob
    assert t["token_id"] == token_id
    assert t["label"] == "lbl"


# ---------------------------------------------------------------------------
# media_secret
# ---------------------------------------------------------------------------


def test_media_secret_is_32_bytes(store):
    secret = store.get_or_create_media_secret()
    assert len(secret) == 32


def test_media_secret_stable_across_instances(tmp_path):
    path = tmp_path / "api_tokens.json"
    s1 = ApiTokenStore(path=path)
    secret1 = s1.get_or_create_media_secret()
    s2 = ApiTokenStore(path=path)
    secret2 = s2.get_or_create_media_secret()
    assert secret1 == secret2


# ---------------------------------------------------------------------------
# atomic persistence (restart survival)
# ---------------------------------------------------------------------------


def test_tokens_persist_across_new_store_instance(tmp_path):
    path = tmp_path / "api_tokens.json"
    s1 = ApiTokenStore(path=path)
    token_id, plaintext = s1.issue(["admin"])

    s2 = ApiTokenStore(path=path)
    entry = s2.resolve(plaintext)
    assert entry is not None
    assert entry["token_id"] == token_id
