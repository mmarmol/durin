import json
import time

from durin.loops import queue


def _ev(content: str, **over) -> dict:
    return {"content": content, "origin": {"channel": "email"}} | over


def test_pop_fresh_on_missing_file_returns_none(tmp_path):
    assert queue.pop_fresh(tmp_path, "l1", 3600) is None


def test_pending_on_missing_file_is_zero(tmp_path):
    assert queue.pending(tmp_path, "l1") == 0


def test_push_then_pop_fresh_fifo_order(tmp_path):
    queue.push(tmp_path, "l1", _ev("first"))
    queue.push(tmp_path, "l1", _ev("second"))
    queue.push(tmp_path, "l1", _ev("third"))
    assert queue.pending(tmp_path, "l1") == 3

    first = queue.pop_fresh(tmp_path, "l1", 3600)
    second = queue.pop_fresh(tmp_path, "l1", 3600)
    assert first["content"] == "first"
    assert second["content"] == "second"
    assert queue.pending(tmp_path, "l1") == 1

    third = queue.pop_fresh(tmp_path, "l1", 3600)
    assert third["content"] == "third"
    assert queue.pending(tmp_path, "l1") == 0
    assert queue.pop_fresh(tmp_path, "l1", 3600) is None


def test_push_stamps_queued_at_when_absent(tmp_path):
    queue.push(tmp_path, "l1", _ev("no-ts"))
    p = tmp_path / "loops" / "queue" / "l1.jsonl"
    rec = json.loads(p.read_text(encoding="utf-8").splitlines()[0])
    assert isinstance(rec.get("queued_at"), float)


def test_push_respects_caller_supplied_queued_at(tmp_path):
    queue.push(tmp_path, "l1", _ev("has-ts", queued_at=12345.0))
    p = tmp_path / "loops" / "queue" / "l1.jsonl"
    rec = json.loads(p.read_text(encoding="utf-8").splitlines()[0])
    assert rec["queued_at"] == 12345.0


def test_ttl_expiry_drops_stale_events(tmp_path):
    now = time.time()
    queue.push(tmp_path, "l1", _ev("stale", queued_at=now - 600))   # 600s old
    queue.push(tmp_path, "l1", _ev("recent", queued_at=now - 100))  # 100s old

    popped = queue.pop_fresh(tmp_path, "l1", 300)  # ttl=300s: "stale" is expired, "recent" is not

    assert popped["content"] == "recent"
    assert queue.pending(tmp_path, "l1") == 0  # both the stale entry AND the popped one are gone


def test_ttl_expiry_with_only_stale_events_returns_none_and_clears(tmp_path):
    queue.push(tmp_path, "l1", _ev("stale", queued_at=time.time() - 999999))

    result = queue.pop_fresh(tmp_path, "l1", 60)

    assert result is None
    assert queue.pending(tmp_path, "l1") == 0


def test_malformed_lines_are_skipped(tmp_path):
    p = tmp_path / "loops" / "queue" / "l1.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "not json\n"
        + json.dumps(_ev("good", queued_at=time.time())) + "\n"
        + "{broken\n"
        + "\n",  # blank line tolerated too
        encoding="utf-8",
    )
    assert queue.pending(tmp_path, "l1") == 1
    popped = queue.pop_fresh(tmp_path, "l1", 3600)
    assert popped["content"] == "good"


def test_different_loops_are_independent(tmp_path):
    queue.push(tmp_path, "l1", _ev("for-l1"))
    queue.push(tmp_path, "l2", _ev("for-l2"))
    assert queue.pending(tmp_path, "l1") == 1
    assert queue.pending(tmp_path, "l2") == 1
    assert queue.pop_fresh(tmp_path, "l1", 3600)["content"] == "for-l1"
    assert queue.pending(tmp_path, "l2") == 1
