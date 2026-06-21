import errno, os
from pathlib import Path
import durin.utils.atomic_write as aw


def test_fsync_dir_writes_content(tmp_path: Path):
    p = tmp_path / "x.json"
    aw.atomic_write_text(p, '{"a":1}', fsync_dir=True)
    assert p.read_text() == '{"a":1}'


def test_exdev_fallback(tmp_path: Path, monkeypatch):
    p = tmp_path / "y.json"
    real_replace = os.replace
    calls = {"n": 0}

    def flaky_replace(a, b):
        if calls["n"] == 0:
            calls["n"] += 1
            raise OSError(errno.EXDEV, "cross-device")
        return real_replace(a, b)

    monkeypatch.setattr(aw.os, "replace", flaky_replace)
    aw.atomic_write_text(p, "hello")
    assert p.read_text() == "hello"
