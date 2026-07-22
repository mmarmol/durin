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
    # No prompt on the node -> no sentence to show.
    assert frames[0]["description"] == ""
    # A run row whose node id is absent from the definition falls back to the id
    # for the label, and to an empty description (no node to read a prompt from) —
    # it must not raise.
    assert frames[1]["label"] == "fix"
    assert frames[1]["description"] == ""


def test_finished_frames_carry_the_prompt_sentence_as_description():
    node = SimpleNamespace(id="judge", title="", prompt="You are the JUDGE. Be strict.",
                           command="", script="")
    runs = [SimpleNamespace(node_id="judge", status="ok", route_label=None, iteration=1, budget=None)]
    assert finished_frames(_wf(judge=node), runs)[0]["description"] == "You are the JUDGE"


def test_finished_frames_treat_persist_failed_as_failed():
    runs = [SimpleNamespace(node_id="a", status="persist_failed", route_label=None, iteration=1, budget=None)]
    assert finished_frames(_wf(), runs)[0]["status"] == "failed"


def test_finished_frames_carry_duration_s():
    """The frame's duration_s is the run row's measured wall-clock time — the
    value both rendering surfaces format as an already-finished node's elapsed."""
    runs = [SimpleNamespace(node_id="scan", status="ok", route_label=None,
                            iteration=1, budget=None, duration_s=155.0)]
    assert finished_frames(_wf(), runs)[0]["duration_s"] == 155.0


def test_finished_frames_duration_s_is_none_when_unmeasured():
    """A run row from a node type that never measures its own duration (e.g. a
    subworkflow or parallel aggregate row) has no duration_s attribute at all.
    The frame must carry None there, not silently coerce the missing
    measurement into a 0-second duration."""
    runs = [SimpleNamespace(node_id="scan", status="ok", route_label=None,
                            iteration=1, budget=None)]
    assert finished_frames(_wf(), runs)[0]["duration_s"] is None


def test_running_frame_marks_the_node_running():
    node = SimpleNamespace(id="judge", title="Judge", prompt="", command="", script="")
    frame = running_frame(node, iteration=2, budget=5)
    assert frame == {
        "id": "judge", "label": "Judge", "description": "", "status": "running",
        "route_label": None, "iteration": 2, "budget": 5, "started_at": None,
        "activity": None, "round": None, "max_rounds": None,
    }


def test_running_frame_carries_the_prompt_sentence_as_description():
    node = SimpleNamespace(id="judge", title="", prompt="You are the JUDGE. Be strict.",
                           command="", script="")
    assert running_frame(node, iteration=1, budget=None)["description"] == "You are the JUDGE"


def test_running_frame_carries_started_at():
    node = SimpleNamespace(id="judge", title="Judge", prompt="", command="", script="")
    assert running_frame(node, iteration=1, budget=None, started_at=1700.5)["started_at"] == 1700.5


def test_running_frame_started_at_defaults_to_none():
    node = SimpleNamespace(id="judge", title="Judge", prompt="", command="", script="")
    assert running_frame(node, iteration=1, budget=None)["started_at"] is None
