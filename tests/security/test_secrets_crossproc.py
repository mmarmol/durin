"""Cross-process safety test for SecretStore.

Two processes each add a DIFFERENT secret concurrently to the same
secrets.json. Without a lock the RMW race drops one secret. With
cross_process_lock wrapping load→mutate→save both survive.

See docs/architecture/concurrency.md for lock-ordering invariants.
"""

from __future__ import annotations

import multiprocessing as mp
import os
from pathlib import Path


def _add(home: str, key: str, val: str) -> None:
    """Worker: put a single secret into the store under DURIN_HOME=home."""
    os.environ["DURIN_HOME"] = home
    # Import after setting env so _default_secrets_path() resolves correctly.
    from durin.security.secrets import SecretStore  # noqa: PLC0415

    store = SecretStore()
    store.put(key, value=val, service="test")
    store.save()


def test_two_processes_no_lost_secret(tmp_path: Path) -> None:
    """Both secrets must survive concurrent puts from separate processes."""
    ctx = mp.get_context("spawn")
    ps = [
        ctx.Process(target=_add, args=(str(tmp_path), f"K{i}", f"V{i}"))
        for i in range(2)
    ]
    for p in ps:
        p.start()
    for p in ps:
        p.join(20)

    os.environ["DURIN_HOME"] = str(tmp_path)
    # Re-import in a fresh store so we read from disk, not a cached singleton.
    from durin.security.secrets import SecretStore  # noqa: PLC0415

    store = SecretStore()
    entry0 = store.get("K0")
    entry1 = store.get("K1")
    assert entry0 is not None and entry0.value == "V0", f"K0 lost — got {entry0}"
    assert entry1 is not None and entry1.value == "V1", f"K1 lost — got {entry1}"
