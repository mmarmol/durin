from durin.cli.gateway_daemon import (
    clear_daemon_runtime,
    daemon_runtime_path,
    read_daemon_runtime_url,
    write_daemon_runtime,
)


def test_runtime_url_roundtrip() -> None:
    assert read_daemon_runtime_url() is None  # nothing written yet (fresh instance)
    write_daemon_runtime(webui_url="http://127.0.0.1:9931/")
    assert read_daemon_runtime_url() == "http://127.0.0.1:9931/"


def test_runtime_url_cleared() -> None:
    write_daemon_runtime(webui_url="http://127.0.0.1:9931/")
    clear_daemon_runtime()
    assert read_daemon_runtime_url() is None


def test_runtime_path_is_instance_relative() -> None:
    from durin.config.home import durin_home

    assert daemon_runtime_path() == durin_home() / "gateway-runtime.json"
