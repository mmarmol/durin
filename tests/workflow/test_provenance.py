import json
from durin.workflow.provenance import node_hash, params_hash, record, load

def test_node_hash_is_stable_and_order_insensitive():
    a = {"id": "n", "prompt": "p", "output_schema": {"x": 1}}
    b = {"output_schema": {"x": 1}, "prompt": "p", "id": "n"}
    assert node_hash(a) == node_hash(b)
    assert node_hash({**a, "prompt": "changed"}) != node_hash(a)

def test_record_and_load_round_trip(tmp_path):
    record(tmp_path, "diagnosis.json", {"run_id": "r1", "node_id": "analyze",
                                        "node_hash": "abc", "model": "glm-5.3"})
    record(tmp_path, "note.json", {"run_id": "r1", "node_id": "draft"})
    got = load(tmp_path)
    assert got["diagnosis.json"]["model"] == "glm-5.3"
    assert set(got) == {"diagnosis.json", "note.json"}

def test_record_overwrites_same_filename(tmp_path):
    record(tmp_path, "a.json", {"run_id": "r1"})
    record(tmp_path, "a.json", {"run_id": "r2"})
    assert load(tmp_path)["a.json"]["run_id"] == "r2"

def test_load_tolerates_missing_and_corrupt(tmp_path):
    assert load(tmp_path) == {}
    (tmp_path / ".provenance.json").write_text("{broken", encoding="utf-8")
    assert load(tmp_path) == {}
