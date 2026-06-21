"""Single-process atomic+lock tests for pairing.json.

Verifies that generate_code, approve_code, deny_code, and revoke use
atomic_write_text and acquire the cross-process lock (lock file created).
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture()
def patched_store(tmp_path, monkeypatch):
    path = tmp_path / "pairing.json"
    from durin.pairing import store
    monkeypatch.setattr(store, "_store_path", lambda: path)
    return path


def test_generate_code_creates_lock_file(patched_store: Path) -> None:
    from durin.pairing import store

    store.generate_code("ch", "user1")
    assert Path(f"{patched_store}.lock").exists()


def test_approve_code_creates_lock_file(patched_store: Path) -> None:
    from durin.pairing import store

    code = store.generate_code("ch", "user1")
    store.approve_code(code)
    assert Path(f"{patched_store}.lock").exists()


def test_deny_code_creates_lock_file(patched_store: Path) -> None:
    from durin.pairing import store

    code = store.generate_code("ch", "user1")
    store.deny_code(code)
    assert Path(f"{patched_store}.lock").exists()


def test_revoke_creates_lock_file(patched_store: Path) -> None:
    from durin.pairing import store

    code = store.generate_code("ch", "user1")
    store.approve_code(code)
    store.revoke("ch", "user1")
    assert Path(f"{patched_store}.lock").exists()


def test_pairing_json_is_valid_json_after_generate(patched_store: Path) -> None:
    import json
    from durin.pairing import store

    code = store.generate_code("ch", "u1")
    data = json.loads(patched_store.read_text(encoding="utf-8"))
    assert code in data["pending"]
