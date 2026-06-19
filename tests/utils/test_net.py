import socket

from durin.utils.net import pick_free_port


def test_returns_preferred_when_free() -> None:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    free = s.getsockname()[1]
    s.close()
    assert pick_free_port("127.0.0.1", free) == free


def test_falls_back_when_taken() -> None:
    held = socket.socket()
    held.bind(("127.0.0.1", 0))
    held.listen(1)  # an ACTIVE listener — the condition a real daemon creates
    taken = held.getsockname()[1]
    try:
        got = pick_free_port("127.0.0.1", taken)
        assert got != taken
        assert got > 0
    finally:
        held.close()
