"""Cross-process test for SecretStore.set_scope_locked.

Without the fix, two concurrent grant operations on the same secret
do an unlocked load→set_scope→save, and the second writer's save
overwrites the first's change — one scope tag is lost.

set_scope_locked wraps load→mutate→save in cross_process_lock so
both tags survive.
"""

from __future__ import annotations

import multiprocessing
import os
from pathlib import Path

import pytest

from durin.security.secrets import SecretStore


def _grant_consumer(secrets_path: str, secret_name: str, consumer: str) -> None:
    """Worker: grant a consumer to a secret using the fully locked path."""
    store = SecretStore(path=Path(secrets_path))
    store.grant_consumer_locked(secret_name, consumer)


def test_concurrent_grants_both_survive(tmp_path: Path) -> None:
    """Two processes grant distinct consumers; both must appear in final scope."""
    secrets_path = tmp_path / "secrets.json"

    # Seed the store with a secret that has an empty scope.
    seed_store = SecretStore(path=secrets_path)
    seed_store.put("API_KEY", value="secret-value", service="provider:test", scope=[])

    ctx = multiprocessing.get_context("spawn")

    p1 = ctx.Process(
        target=_grant_consumer,
        args=(str(secrets_path), "API_KEY", "exec"),
    )
    p2 = ctx.Process(
        target=_grant_consumer,
        args=(str(secrets_path), "API_KEY", "skill:deploy"),
    )

    p1.start()
    p2.start()
    p1.join(timeout=10)
    p2.join(timeout=10)

    assert p1.exitcode == 0, f"process 1 exited with code {p1.exitcode}"
    assert p2.exitcode == 0, f"process 2 exited with code {p2.exitcode}"

    final = SecretStore(path=secrets_path).load()
    scope = set(final.get("API_KEY").scope)
    assert "exec" in scope, f"'exec' lost from scope; got {scope}"
    assert "skill:deploy" in scope, f"'skill:deploy' lost from scope; got {scope}"
