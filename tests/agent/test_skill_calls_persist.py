import json

from durin.session.manager import SessionManager
from durin.session.session_meta import meta_path_for


def test_skill_calls_round_trip_through_meta_sidecar(tmp_path):
    sm = SessionManager(tmp_path)
    s = sm.get_or_create("websocket:abc")
    s.add_message("user", "do a thing")
    s.metadata["skill_calls"] = [{"skill": "git-helper", "op": "read"}]
    sm.save(s)
    sm._cache.clear()
    reloaded = sm.get_or_create("websocket:abc")
    assert reloaded.metadata.get("skill_calls") == [{"skill": "git-helper", "op": "read"}]


def test_skill_calls_live_in_the_meta_sidecar_not_line_0(tmp_path):
    sm = SessionManager(tmp_path)
    s = sm.get_or_create("websocket:abc")
    s.add_message("user", "x")
    s.metadata["skill_calls"] = [{"skill": "a", "op": "edit"}]
    sm.save(s)
    meta = json.loads(meta_path_for("websocket:abc", sm.sessions_dir).read_text())
    assert meta["derived"]["skill_calls"] == [{"skill": "a", "op": "edit"}]
