from types import SimpleNamespace
from durin.workflow.engine import NodeRunResponse
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
    (tmp_path / ".provenance.json").write_bytes(b'{"model": "gl\xc3"}')
    assert load(tmp_path) == {}

def test_params_hash_is_stable():
    gen1 = SimpleNamespace(max_tokens=1000, temperature=0.7, reasoning_effort=None, top_p=0.9, top_k=50)
    gen2 = SimpleNamespace(top_k=50, top_p=0.9, temperature=0.7, max_tokens=1000, reasoning_effort=None)
    assert params_hash(gen1) == params_hash(gen2)
    gen3 = SimpleNamespace(max_tokens=2000, temperature=0.7, reasoning_effort=None, top_p=0.9, top_k=50)
    assert params_hash(gen1) != params_hash(gen3)

def test_node_run_response_provenance_fields_default_to_none():
    resp = NodeRunResponse(output="x")
    assert resp.model is None
    assert resp.provider is None
    assert resp.params_hash is None

def test_node_run_response_carries_resolved_model(tmp_path):
    # reuse the harness pattern from tests/workflow/test_node_runner_structured.py
    from tests.workflow.test_node_runner_structured import _req, _runner_with_deliver, _schema_node
    nr, provider = _runner_with_deliver(tmp_path, [{"queries": ["a"]}])
    resp = nr(_req(_schema_node()))
    assert resp.model == "test-model"

def test_worknode_retains_raw_spec_for_hashing():
    from durin.workflow.provenance import node_hash
    from durin.workflow.spec import parse_workflow
    src = {"id": "plan", "kind": "work", "prompt": "P1", "output_schema": {"type": "object"}, "next": None}
    wf = parse_workflow({"name": "d", "start": "plan", "nodes": [src]})
    raw = wf.nodes["plan"].raw
    assert raw.get("prompt") == "P1"
    h1 = node_hash(raw)
    src2 = dict(src, prompt="P2")
    wf2 = parse_workflow({"name": "d", "start": "plan", "nodes": [src2]})
    assert node_hash(wf2.nodes["plan"].raw) != h1


def test_reuse_hash_ignores_routing_only_edits():
    from durin.workflow.provenance import reuse_hash
    a = {"id": "n", "prompt": "p", "next": "b", "on_fail": None}
    b = {"id": "n", "prompt": "p", "next": "z", "on_fail": "retry"}
    assert reuse_hash(a) == reuse_hash(b)


def test_reuse_hash_changes_when_prompt_changes():
    from durin.workflow.provenance import reuse_hash
    a = {"id": "n", "prompt": "p"}
    b = {"id": "n", "prompt": "p2"}
    assert reuse_hash(a) != reuse_hash(b)


def test_reuse_hash_is_projection_of_node_hash():
    # reuse_hash must equal node_hash of the REUSE_RELEVANT_KEYS projection, not
    # the whole node dict — proving it does not accidentally hash routing fields.
    from durin.workflow.provenance import REUSE_RELEVANT_KEYS, node_hash, reuse_hash
    src = {"id": "n", "prompt": "p", "next": "b", "detached": True}
    projection = {k: src.get(k) for k in REUSE_RELEVANT_KEYS}
    assert reuse_hash(src) == node_hash(projection)


def test_durin_version_matches_installed_metadata():
    import importlib.metadata

    from durin.workflow.provenance import durin_version
    assert durin_version() == importlib.metadata.version("durin-agent")


def test_durin_version_returns_none_when_unresolvable(monkeypatch):
    import importlib.metadata

    from durin.workflow import provenance

    def _raise(name):
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", _raise)
    assert provenance.durin_version() is None
