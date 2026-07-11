import pytest
from durin.loops.spec import LoopNotFound, parse_loop
from durin.loops.store import delete_loop, list_loops, load_loop, loops_dir, save_loop


def _spec(name="certs"):
    return parse_loop({"name": name, "workflow": "renew-certs", "goal": {"intent": "certs renewed"}})


def test_save_load_roundtrip(tmp_path):
    save_loop(tmp_path, _spec())
    assert (loops_dir(tmp_path) / "certs.json").exists()
    assert load_loop(tmp_path, "certs") == _spec()


def test_list_sorted_and_skips_malformed(tmp_path):
    save_loop(tmp_path, _spec("bbb"))
    save_loop(tmp_path, _spec("aaa"))
    (loops_dir(tmp_path) / "broken.json").write_text("{not json")
    assert [s.name for s in list_loops(tmp_path)] == ["aaa", "bbb"]


def test_load_missing_raises(tmp_path):
    with pytest.raises(LoopNotFound):
        load_loop(tmp_path, "nope")


def test_delete(tmp_path):
    save_loop(tmp_path, _spec())
    delete_loop(tmp_path, "certs")
    assert list_loops(tmp_path) == []
    with pytest.raises(LoopNotFound):
        delete_loop(tmp_path, "certs")
