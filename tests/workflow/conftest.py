"""Shared fixtures for the workflow tests."""
import pytest

# Imported by its full name, the way the test modules import it, so this
# fixture and the tests share one registry of started processes.
from tests.workflow import crash_harness


@pytest.fixture(autouse=True)
def _reap_crash_harness_processes():
    """No process a test started through the crash harness outlives the test."""
    yield
    crash_harness.reap()
