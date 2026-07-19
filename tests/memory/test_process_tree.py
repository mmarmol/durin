"""RSS sampling helpers (dream observability + supervisor watchdog)."""
from __future__ import annotations

import os
import subprocess
import sys

from durin.utils.process_tree import process_rss_mb, tree_rss_mb


def test_own_process_rss_is_positive() -> None:
    assert process_rss_mb() > 0


def test_unknown_pid_reports_zero() -> None:
    assert process_rss_mb(2**22 + 12345) == 0.0
    assert tree_rss_mb(2**22 + 12345) == (0.0, 0.0)


def test_tree_rss_counts_a_live_child() -> None:
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; x = 'a' * (30 * 2**20); time.sleep(30)"],
    )
    try:
        import time

        # The child allocates ~30MB then sleeps; poll briefly until visible.
        for _ in range(50):
            _root, descendants = tree_rss_mb(os.getpid())
            if descendants >= 20:
                break
            time.sleep(0.1)
        root, descendants = tree_rss_mb(os.getpid())
        assert root > 0
        assert descendants >= 20
    finally:
        child.kill()
        child.wait()
