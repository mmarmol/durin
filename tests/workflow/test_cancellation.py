"""The workflow cancellation registry: request / is_cancelled / clear lifecycle."""

from __future__ import annotations

from durin.workflow import cancellation


def test_unknown_run_is_not_cancelled():
    assert cancellation.is_cancelled("nope-unknown") is False


def test_request_then_is_cancelled_then_clear():
    run_id = "test-run-abc123"
    assert cancellation.is_cancelled(run_id) is False
    cancellation.request_cancel(run_id)
    assert cancellation.is_cancelled(run_id) is True
    cancellation.clear(run_id)
    assert cancellation.is_cancelled(run_id) is False


def test_clear_is_idempotent_when_absent():
    cancellation.clear("never-registered")  # must not raise


def test_runs_are_independent():
    cancellation.request_cancel("run-A")
    try:
        assert cancellation.is_cancelled("run-A") is True
        assert cancellation.is_cancelled("run-B") is False
    finally:
        cancellation.clear("run-A")


def test_graceful_request_is_not_hard():
    run_id = "run-graceful"
    cancellation.request_cancel(run_id)
    try:
        assert cancellation.is_cancelled(run_id) is True
        assert cancellation.is_hard_cancelled(run_id) is False
    finally:
        cancellation.clear(run_id)


def test_hard_request_sets_both():
    run_id = "run-hard"
    cancellation.request_cancel(run_id, hard=True)
    try:
        assert cancellation.is_cancelled(run_id) is True
        assert cancellation.is_hard_cancelled(run_id) is True
    finally:
        cancellation.clear(run_id)


def test_repeat_request_upgrades_but_never_downgrades():
    run_id = "run-upgrade"
    cancellation.request_cancel(run_id)
    cancellation.request_cancel(run_id, hard=True)
    try:
        assert cancellation.is_hard_cancelled(run_id) is True
        # A later graceful request must not downgrade the hard mode.
        cancellation.request_cancel(run_id)
        assert cancellation.is_hard_cancelled(run_id) is True
    finally:
        cancellation.clear(run_id)
