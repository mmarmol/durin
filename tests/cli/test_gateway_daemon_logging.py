import importlib
from pathlib import Path


def test_boot_path_is_sibling_of_log(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    import durin.cli.gateway_daemon as gd
    importlib.reload(gd)
    assert gd.daemon_boot_logs_path().name == "gateway.boot.log"
    assert gd.daemon_boot_logs_path().parent == gd.daemon_logs_path().parent
    assert gd.GATEWAY_LOG_FILE_ENV == "DURIN_GATEWAY_LOG_FILE"
