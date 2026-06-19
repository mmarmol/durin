import socket

from durin.utils.net import port_is_available


def test_available_when_free() -> None:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    free = s.getsockname()[1]
    s.close()
    assert port_is_available("127.0.0.1", free) is True


def test_taken_when_active_listener() -> None:
    held = socket.socket()
    held.bind(("127.0.0.1", 0))
    held.listen(1)  # an ACTIVE listener — the real collision condition
    taken = held.getsockname()[1]
    try:
        assert port_is_available("127.0.0.1", taken) is False
    finally:
        held.close()
