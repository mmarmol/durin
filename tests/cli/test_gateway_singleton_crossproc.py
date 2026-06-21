"""Cross-process test for the held-flock gateway singleton.

Spawns a real separate process that holds acquire_gateway_singleton(), then
asserts a second call in the parent raises AlreadyRunningError. Verifies the
lock is released when the holder exits (so a third acquire in the parent succeeds).
"""

import multiprocessing as mp
import os
from pathlib import Path


def _hold(home: str, started, release):
    os.environ["DURIN_HOME"] = home
    from durin.cli.gateway_daemon import acquire_gateway_singleton

    h = acquire_gateway_singleton()  # noqa: F841  (kept alive intentionally)
    started.set()
    release.wait(5.0)


def test_second_gateway_refused(tmp_path: Path):
    ctx = mp.get_context("spawn")
    started, release = ctx.Event(), ctx.Event()
    p = ctx.Process(target=_hold, args=(str(tmp_path), started, release))
    p.start()
    assert started.wait(5.0), "holder process did not signal ready"

    os.environ["DURIN_HOME"] = str(tmp_path)
    from durin.cli.gateway_daemon import AlreadyRunningError, acquire_gateway_singleton
    import pytest

    with pytest.raises(AlreadyRunningError):
        acquire_gateway_singleton()

    release.set()
    p.join(10)
    assert p.exitcode == 0, f"holder process exited with code {p.exitcode}"

    # After the holder exits the lock is released; a fresh acquire must succeed.
    # Reset the module-global so this process can acquire again.
    import durin.cli.gateway_daemon as gd

    gd._singleton_handle = None
    h2 = acquire_gateway_singleton()
    assert h2 is not None
    # Clean up so we don't bleed into other tests.
    h2.close()
    gd._singleton_handle = None
