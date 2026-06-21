import threading
from pathlib import Path
import pytest
from durin.utils.file_lock import cross_process_lock


def test_acquire_release_creates_sidecar(tmp_path: Path):
    target = tmp_path / "data.json"
    with cross_process_lock(target):
        assert (tmp_path / "data.json.lock").exists()


def test_reentrant_same_thread_same_path(tmp_path: Path):
    target = tmp_path / "data.json"
    with cross_process_lock(target):
        with cross_process_lock(target):  # must NOT deadlock
            pass


def test_timeout_when_held_by_other_thread(tmp_path: Path):
    target = tmp_path / "data.json"
    started = threading.Event()
    release = threading.Event()

    def holder():
        with cross_process_lock(target):
            started.set()
            release.wait(2.0)

    t = threading.Thread(target=holder)
    t.start()
    assert started.wait(2.0)
    with pytest.raises(TimeoutError):
        with cross_process_lock(target, timeout=0.3):
            pass
    release.set()
    t.join()
