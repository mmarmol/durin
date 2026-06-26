"""Workflow node/run sessions are internal execution traces — persisted only for the
run-detail view, never user chats. They stay out of the session list and earn no .md,
so they never reach the FTS index or the memory entity graph (both read the .md)."""

from durin.session.manager import Session, SessionManager, is_workflow_session


def test_is_workflow_session_discriminates_by_key_prefix():
    assert is_workflow_session("workflow:run1:plan:1")
    assert is_workflow_session("workflow:run1:plan:1:0")   # fan-out worker
    assert is_workflow_session("workflow:run1:root")       # synthetic run-root
    assert not is_workflow_session("websocket:abc")
    assert not is_workflow_session("subagent:t1")


def test_list_sessions_excludes_workflow_sessions(tmp_path):
    sm = SessionManager(workspace=tmp_path)
    sm.save(Session(key="websocket:abc", messages=[{"role": "user", "content": "hi"}]))
    sm.save(Session(key="workflow:run1:plan:1", messages=[{"role": "user", "content": "node"}]))
    sm.save(Session(key="workflow:run1:root"))

    keys = [s["key"] for s in sm.list_sessions()]
    assert "websocket:abc" in keys
    assert not any(k.startswith("workflow:") for k in keys)


def test_save_persists_jsonl_but_skips_md_index_for_workflow_sessions(tmp_path, monkeypatch):
    # Patch the .md regeneration (the single artifact FTS + the entity dream consume) so we
    # can assert it runs for a user session but is skipped for a workflow node session.
    import durin.memory.session_md as smd

    regenerated: list[str] = []
    monkeypatch.setattr(smd, "regenerate_session_md", lambda path: regenerated.append(str(path)))

    sm = SessionManager(workspace=tmp_path)
    sm.save(Session(key="websocket:abc", messages=[{"role": "user", "content": "hi"}]))
    sm.save(Session(key="workflow:run1:plan:1", messages=[{"role": "user", "content": "node"}]))

    assert any("websocket_abc" in p for p in regenerated)     # user chat → indexed
    assert not any("workflow" in p for p in regenerated)      # workflow node → not indexed
    # but the workflow session's transcript IS on disk, so the run-detail view can read it
    assert (sm.sessions_dir / "workflow_run1_plan_1.jsonl").exists()


def test_memory_graph_excludes_workflow_session_nodes(tmp_path):
    # The graph builds a session node per *.jsonl (which workflow sessions keep on disk),
    # so it must filter them at the walk — not rely on the .md being absent.
    from durin.memory.graph import build_memory_graph

    sm = SessionManager(workspace=tmp_path)
    sm.save(Session(key="websocket:abc", messages=[{"role": "user", "content": "hi"}]))
    sm.save(Session(key="workflow:run1:plan:1", messages=[{"role": "user", "content": "node"}]))

    nodes = build_memory_graph(tmp_path).get("nodes", [])
    session_nodes = [n for n in nodes if isinstance(n, dict) and n.get("type") == "session"]
    assert any("websocket" in n["id"] for n in session_nodes)
    assert not any("workflow" in n["id"] for n in session_nodes)
