from types import SimpleNamespace

from durin.workflow.progress import finished_frames, running_frame


def _wf(**nodes):
    return SimpleNamespace(nodes=nodes)


def test_finished_frames_map_status_and_label():
    node = SimpleNamespace(id="scan", title="Scan the repo", prompt="", command="", script="")
    runs = [
        SimpleNamespace(node_id="scan", status="ok", route_label="pass", iteration=1, budget=3),
        SimpleNamespace(node_id="fix", status="node_failed", route_label=None, iteration=1, budget=None),
    ]
    frames = finished_frames(_wf(scan=node), runs)
    assert [f["status"] for f in frames] == ["done", "failed"]
    assert frames[0]["label"] == "Scan the repo"
    assert frames[0]["route_label"] == "pass"
    assert frames[0]["budget"] == 3
    # A run row whose node id is absent from the definition falls back to the id.
    assert frames[1]["label"] == "fix"


def test_finished_frames_treat_persist_failed_as_failed():
    runs = [SimpleNamespace(node_id="a", status="persist_failed", route_label=None, iteration=1, budget=None)]
    assert finished_frames(_wf(), runs)[0]["status"] == "failed"


def test_running_frame_marks_the_node_running():
    node = SimpleNamespace(id="judge", title="Judge", prompt="", command="", script="")
    frame = running_frame(node, iteration=2, budget=5)
    assert frame == {
        "id": "judge", "label": "Judge", "status": "running",
        "route_label": None, "iteration": 2, "budget": 5, "started_at": None,
    }


def test_running_frame_carries_started_at():
    node = SimpleNamespace(id="judge", title="Judge", prompt="", command="", script="")
    assert running_frame(node, iteration=1, budget=None, started_at=1700.5)["started_at"] == 1700.5


def test_running_frame_started_at_defaults_to_none():
    node = SimpleNamespace(id="judge", title="Judge", prompt="", command="", script="")
    assert running_frame(node, iteration=1, budget=None)["started_at"] is None
