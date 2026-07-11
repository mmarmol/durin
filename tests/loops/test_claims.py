"""Tests for claims index: register, lookup, release, release_run, prune."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from durin.loops.claims import claims_path, lookup, prune, register, release, release_run


@pytest.fixture
def temp_ws(tmp_path: Path) -> Path:
    """Temporary workspace for test isolation."""
    return tmp_path


def test_claims_path(temp_ws: Path) -> None:
    """claims_path returns <workspace>/loops/claims.json."""
    expected = temp_ws / "loops" / "claims.json"
    assert claims_path(temp_ws) == expected


def test_register_and_lookup(temp_ws: Path) -> None:
    """Register a claim and look it up."""
    key = "thread-digest-1"
    loop = "loop-a"
    run_id = "run-123"

    register(temp_ws, key=key, loop=loop, run_id=run_id)

    claim = lookup(temp_ws, key)
    assert claim is not None
    assert claim["loop"] == loop
    assert claim["run_id"] == run_id
    assert "registered_at" in claim
    assert isinstance(claim["registered_at"], float)


def test_lookup_nonexistent(temp_ws: Path) -> None:
    """Lookup a key that doesn't exist returns None."""
    result = lookup(temp_ws, "nonexistent-key")
    assert result is None


def test_release_removes_claim(temp_ws: Path) -> None:
    """Release removes a claim."""
    key = "thread-digest-1"
    register(temp_ws, key=key, loop="loop-a", run_id="run-123")
    assert lookup(temp_ws, key) is not None

    release(temp_ws, key)
    assert lookup(temp_ws, key) is None


def test_release_idempotent(temp_ws: Path) -> None:
    """Release is idempotent (no error if key doesn't exist)."""
    release(temp_ws, "nonexistent-key")  # should not raise
    release(temp_ws, "nonexistent-key")  # should not raise


def test_release_run_removes_all_claims_for_run(temp_ws: Path) -> None:
    """release_run removes all claims held by a run."""
    loop_a = "loop-a"
    run_id = "run-123"

    # Register multiple claims for the same run
    register(temp_ws, key="key-1", loop=loop_a, run_id=run_id)
    register(temp_ws, key="key-2", loop=loop_a, run_id=run_id)
    register(temp_ws, key="key-3", loop="loop-b", run_id="other-run")

    # Release run
    release_run(temp_ws, loop=loop_a, run_id=run_id)

    # Claims for that run should be gone
    assert lookup(temp_ws, "key-1") is None
    assert lookup(temp_ws, "key-2") is None

    # Other run's claim should remain
    assert lookup(temp_ws, "key-3") is not None


def test_release_run_idempotent(temp_ws: Path) -> None:
    """release_run is idempotent (no error if run doesn't exist)."""
    release_run(temp_ws, loop="loop-a", run_id="nonexistent-run")  # should not raise
    release_run(temp_ws, loop="loop-a", run_id="nonexistent-run")  # should not raise


def test_prune_expires_old_claims(temp_ws: Path) -> None:
    """prune removes claims older than max_age_s."""
    # Register a claim with old timestamp
    key_old = "old-key"
    old_time = time.time() - 100  # 100 seconds ago
    register(temp_ws, key=key_old, loop="loop-a", run_id="run-1")

    # Manually update the timestamp to make it old
    claims_file = claims_path(temp_ws)
    import json

    data = json.loads(claims_file.read_text(encoding="utf-8"))
    data[key_old]["registered_at"] = old_time
    claims_file.write_text(json.dumps(data), encoding="utf-8")

    # Register a fresh claim
    key_fresh = "fresh-key"
    register(temp_ws, key=key_fresh, loop="loop-a", run_id="run-2")

    # Prune with 50 second max age
    released = prune(temp_ws, max_age_s=50)

    # Old key should be pruned
    assert key_old in released
    assert lookup(temp_ws, key_old) is None

    # Fresh key should remain
    assert lookup(temp_ws, key_fresh) is not None


def test_prune_keeps_fresh_claims(temp_ws: Path) -> None:
    """prune keeps claims newer than max_age_s."""
    key = "fresh-key"
    register(temp_ws, key=key, loop="loop-a", run_id="run-1")

    # Prune with 1000 second max age
    released = prune(temp_ws, max_age_s=1000)

    # Fresh key should not be pruned
    assert key not in released
    assert lookup(temp_ws, key) is not None


def test_prune_returns_empty_when_no_claims(temp_ws: Path) -> None:
    """prune on empty claims file returns empty list."""
    released = prune(temp_ws, max_age_s=50)
    assert released == []


def test_prune_returns_released_keys(temp_ws: Path) -> None:
    """prune returns list of keys that were released."""
    import json

    # Register multiple claims with old timestamps
    key1 = "old-key-1"
    key2 = "old-key-2"
    key3 = "fresh-key"

    register(temp_ws, key=key1, loop="loop-a", run_id="run-1")
    register(temp_ws, key=key2, loop="loop-a", run_id="run-2")
    register(temp_ws, key=key3, loop="loop-a", run_id="run-3")

    # Make key1 and key2 old
    claims_file = claims_path(temp_ws)
    data = json.loads(claims_file.read_text(encoding="utf-8"))
    old_time = time.time() - 100
    data[key1]["registered_at"] = old_time
    data[key2]["registered_at"] = old_time
    claims_file.write_text(json.dumps(data), encoding="utf-8")

    # Prune
    released = prune(temp_ws, max_age_s=50)

    # Check returned keys
    assert sorted(released) == sorted([key1, key2])


def test_multiple_registers_overwrite(temp_ws: Path) -> None:
    """Registering the same key twice updates the claim."""
    key = "thread-digest-1"

    register(temp_ws, key=key, loop="loop-a", run_id="run-1")
    claim1 = lookup(temp_ws, key)
    time1 = claim1["registered_at"]

    # Small delay to ensure different timestamp
    time.sleep(0.01)

    register(temp_ws, key=key, loop="loop-b", run_id="run-2")
    claim2 = lookup(temp_ws, key)
    time2 = claim2["registered_at"]

    # New registration should have new loop, run_id, and timestamp
    assert claim2["loop"] == "loop-b"
    assert claim2["run_id"] == "run-2"
    assert time2 > time1


def test_claims_file_malformed_tolerance(temp_ws: Path) -> None:
    """Malformed claims file is handled gracefully."""
    claims_file = claims_path(temp_ws)
    claims_file.parent.mkdir(parents=True, exist_ok=True)
    claims_file.write_text("invalid json", encoding="utf-8")

    # Lookup should return None (file is skipped)
    result = lookup(temp_ws, "any-key")
    assert result is None

    # Register should overwrite with valid JSON
    register(temp_ws, key="key-1", loop="loop-a", run_id="run-1")
    assert lookup(temp_ws, "key-1") is not None


def test_claims_file_contains_null_json(temp_ws: Path) -> None:
    """File containing null (valid JSON, non-dict) is tolerated.

    lookup returns None and register works after.
    """
    claims_file = claims_path(temp_ws)
    claims_file.parent.mkdir(parents=True, exist_ok=True)
    claims_file.write_text("null", encoding="utf-8")

    # Lookup should return None (top-level is not a dict)
    result = lookup(temp_ws, "any-key")
    assert result is None

    # Register should work and overwrite with valid JSON
    register(temp_ws, key="key-1", loop="loop-a", run_id="run-1")
    assert lookup(temp_ws, "key-1") is not None
    claim = lookup(temp_ws, "key-1")
    assert claim["loop"] == "loop-a"
    assert claim["run_id"] == "run-1"


def test_claims_file_non_dict_value_entries(temp_ws: Path) -> None:
    """File with non-dict value entries is tolerated.

    prune and release_run don't crash and bad entry is ignored.
    """
    import json

    claims_file = claims_path(temp_ws)
    claims_file.parent.mkdir(parents=True, exist_ok=True)

    # Write a file with mixed valid and invalid entries, with old timestamp
    old_time = time.time() - 100  # 100 seconds ago
    data = {
        "good-key": {"loop": "loop-a", "run_id": "run-1", "registered_at": old_time},
        "bad-key-string": "not-a-dict",  # Non-dict value
        "bad-key-null": None,  # Null value
        "bad-key-list": [1, 2, 3],  # List value
    }
    claims_file.write_text(json.dumps(data), encoding="utf-8")

    # Lookup on bad entry should return None
    assert lookup(temp_ws, "bad-key-string") is None
    assert lookup(temp_ws, "bad-key-null") is None
    assert lookup(temp_ws, "bad-key-list") is None

    # Good entry should still be found
    assert lookup(temp_ws, "good-key") is not None

    # prune should not crash on bad entries
    released = prune(temp_ws, max_age_s=50)
    assert "good-key" in released  # Good entry is old, gets pruned
    assert "bad-key-string" not in released  # Bad entries are silently dropped
    assert "bad-key-null" not in released
    assert "bad-key-list" not in released

    # release_run should not crash on bad entries
    claims_file.write_text(json.dumps(data), encoding="utf-8")
    release_run(temp_ws, loop="loop-a", run_id="run-1")
    # Good entry should be removed, bad entries should still be filtered out
    assert lookup(temp_ws, "good-key") is None
    assert lookup(temp_ws, "bad-key-string") is None


def test_register_double_claim_last_writer_wins(temp_ws: Path) -> None:
    """Registering the same key for a different (loop, run_id) overwrites —
    last claim wins. A warning is logged (best-effort, not asserted here)."""
    key = "thread-double-claim"

    register(temp_ws, key=key, loop="loop-a", run_id="run-A")
    register(temp_ws, key=key, loop="loop-b", run_id="run-B")

    claim = lookup(temp_ws, key)
    assert claim is not None
    assert claim["loop"] == "loop-b"
    assert claim["run_id"] == "run-B"


def test_claims_file_non_utf8_bytes(temp_ws: Path) -> None:
    """File with non-UTF-8 bytes is tolerated.

    lookup returns None on UnicodeDecodeError.
    """
    claims_file = claims_path(temp_ws)
    claims_file.parent.mkdir(parents=True, exist_ok=True)

    # Write non-UTF-8 bytes
    claims_file.write_bytes(b"\xff\xfe invalid bytes")

    # Lookup should return None (file is skipped)
    result = lookup(temp_ws, "any-key")
    assert result is None

    # Register should work after
    register(temp_ws, key="key-1", loop="loop-a", run_id="run-1")
    assert lookup(temp_ws, "key-1") is not None
