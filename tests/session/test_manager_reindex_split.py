from durin.session.manager import SessionManager


def _mk(mgr):
    s = mgr.get_or_create("cli:reindex-split")
    s.add_message("user", "hello")
    s.add_message("assistant", "hi there")
    return s


def test_save_no_reindex_skips_md(tmp_path):
    mgr = SessionManager(tmp_path)
    s = _mk(mgr)
    mgr.save(s, reindex=False)
    jsonl = mgr._get_session_path(s.key)
    assert jsonl.exists()                       # durable write happened
    assert not jsonl.with_suffix(".md").exists()  # .md NOT regenerated


def test_reindex_session_produces_md(tmp_path):
    mgr = SessionManager(tmp_path)
    s = _mk(mgr)
    mgr.save(s, reindex=False)
    mgr.reindex_session(s.key)
    assert mgr._get_session_path(s.key).with_suffix(".md").exists()


def test_default_save_still_reindexes(tmp_path):
    mgr = SessionManager(tmp_path)
    s = _mk(mgr)
    mgr.save(s)  # default reindex=True — unchanged behavior
    assert mgr._get_session_path(s.key).with_suffix(".md").exists()
