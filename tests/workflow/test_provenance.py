from types import SimpleNamespace
from durin.workflow.engine import NodeRunResponse
from durin.workflow.provenance import (
    content_sha256, drop, input_hash, load, node_hash, params_hash, record,
)

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

def test_drop_removes_only_the_named_entry(tmp_path):
    record(tmp_path, "a.json", {"run_id": "r1"})
    record(tmp_path, "b.json", {"run_id": "r2"})
    drop(tmp_path, "a.json")
    assert load(tmp_path) == {"b.json": {"run_id": "r2"}}

def test_drop_tolerates_missing_entry_and_missing_file(tmp_path):
    drop(tmp_path, "nope.json")            # no .provenance.json at all yet
    assert load(tmp_path) == {}
    record(tmp_path, "a.json", {"run_id": "r1"})
    drop(tmp_path, "does-not-exist.json")  # entry never existed
    assert load(tmp_path) == {"a.json": {"run_id": "r1"}}

def test_drop_never_raises_on_an_unwritable_path(tmp_path):
    # Failure-suppressed by contract: a cleanup failure must never compound the
    # record() failure it exists to contain.
    drop(tmp_path / "does" / "not" / "exist", "a.json")

def test_params_hash_is_stable():
    gen1 = SimpleNamespace(max_tokens=1000, temperature=0.7, reasoning_effort=None, top_p=0.9, top_k=50)
    gen2 = SimpleNamespace(top_k=50, top_p=0.9, temperature=0.7, max_tokens=1000, reasoning_effort=None)
    assert params_hash(gen1) == params_hash(gen2)
    gen3 = SimpleNamespace(max_tokens=2000, temperature=0.7, reasoning_effort=None, top_p=0.9, top_k=50)
    assert params_hash(gen1) != params_hash(gen3)

def test_input_hash_is_stable_and_sensitive_to_either_half():
    assert input_hash("t", "u") == input_hash("t", "u")
    assert input_hash("t", "u") != input_hash("t", "different")
    assert input_hash("t", "u") != input_hash("different", "u")

def test_input_hash_treats_none_upstream_distinctly_from_empty_string():
    assert input_hash("t", None) != input_hash("t", "")

def test_content_sha256_is_stable_and_sensitive_to_content():
    assert content_sha256("x") == content_sha256("x")
    assert content_sha256("x") != content_sha256("y")

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


def test_reuse_hash_is_projection_excluding_denylist():
    # reuse_hash must equal node_hash of raw MINUS REUSE_IGNORED_KEYS — a denylist,
    # not an allowlist, so a key this test doesn't even know about still hashes.
    from durin.workflow.provenance import REUSE_IGNORED_KEYS, node_hash, reuse_hash
    src = {"id": "n", "prompt": "p", "next": "b", "detached": True, "inputs_from": ["a"]}
    projection = {k: v for k, v in src.items() if k not in REUSE_IGNORED_KEYS}
    assert reuse_hash(src) == node_hash(projection)


def test_reuse_hash_changes_when_inputs_from_changes():
    # C1: inputs_from composes the node's ENTIRE input via the engine — the
    # allowlist used to miss this, letting a producer swap under a stable hash.
    from durin.workflow.provenance import reuse_hash
    a = {"id": "n", "prompt": "p", "inputs_from": ["x"]}
    b = {"id": "n", "prompt": "p", "inputs_from": ["y"]}
    assert reuse_hash(a) != reuse_hash(b)


def test_reuse_hash_is_sensitive_to_previously_missed_content_fields():
    # C1 names these as content-determining fields the old allowlist missed.
    from durin.workflow.provenance import reuse_hash
    base = {"id": "n", "prompt": "p"}
    for key, value in (
        ("mcps", ["server-a"]),
        ("context", "shared"),
        ("session", "persistent"),
        ("max_reentries", 2),
        ("reentry_prompt", "steer"),
    ):
        assert reuse_hash(base) != reuse_hash({**base, key: value}), key


def test_reuse_hash_ignores_next_max_visits_and_reuse_flag():
    from durin.workflow.provenance import reuse_hash
    a = {"id": "n", "prompt": "p", "next": "b", "max_visits": 3, "reuse": None}
    b = {"id": "n", "prompt": "p", "next": "z", "max_visits": 9, "reuse": "if-unchanged"}
    assert reuse_hash(a) == reuse_hash(b)


def test_reuse_hash_is_sensitive_to_unknown_future_keys():
    # Conservative by default: a denylist only ignores keys it explicitly names,
    # so a key nobody anticipated still invalidates reuse instead of being
    # silently dropped the way an allowlist would drop it.
    from durin.workflow.provenance import reuse_hash
    a = {"id": "n", "prompt": "p"}
    b = {"id": "n", "prompt": "p", "some_future_field": "value"}
    assert reuse_hash(a) != reuse_hash(b)


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


def test_node_identity_returns_raw_when_present():
    # A WorkNode's raw spec dict IS its identity — no fallback needed.
    from durin.workflow.provenance import node_identity
    n = SimpleNamespace(raw={"id": "x", "prompt": "p"}, id="x")
    assert node_identity(n) == {"id": "x", "prompt": "p"}


def test_node_identity_falls_back_to_dataclass_fields_when_raw_absent():
    # ScriptNode carries no `raw` field (only WorkNode does) — node_identity must
    # still produce a real identity from the parsed dataclass, not an empty dict.
    from durin.workflow.provenance import node_identity
    from durin.workflow.spec import parse_workflow
    wf = parse_workflow({
        "name": "w", "start": "b",
        "nodes": [{"id": "b", "kind": "script", "command": "echo hi", "next": None}],
    })
    script_node = wf.nodes["b"]
    assert not getattr(script_node, "raw", None)
    identity = node_identity(script_node)
    assert identity["command"] == "echo hi"


def test_node_identity_is_sensitive_to_script_command():
    from durin.workflow.provenance import node_hash, node_identity
    from durin.workflow.spec import parse_workflow

    def _script(command):
        wf = parse_workflow({
            "name": "w", "start": "b",
            "nodes": [{"id": "b", "kind": "script", "command": command, "next": None}],
        })
        return wf.nodes["b"]

    h1 = node_hash(node_identity(_script("echo hi")))
    h2 = node_hash(node_identity(_script("echo bye")))
    assert h1 != h2
