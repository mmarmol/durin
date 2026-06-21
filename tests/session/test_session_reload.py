from pathlib import Path
from durin.session.manager import SessionManager


def test_reload_picks_up_foreign_write(tmp_path: Path):
    a = SessionManager(tmp_path)
    b = SessionManager(tmp_path)            # simulates a second process's manager
    s_a = a.get_or_create("websocket:x")
    s_a.add_message("user", "hi")
    a.save(s_a)

    s_b = b.get_or_create("websocket:x")    # b caches its view
    assert len(s_b.messages) == 1

    s_a.add_message("assistant", "there")   # a appends + saves again
    a.save(s_a)

    # Without reload, b is stale (split-brain). reload() must see a's new turn.
    s_b2 = b.reload("websocket:x")
    assert len(s_b2.messages) == 2
    assert s_b2.messages[-1]["content"] == "there"
