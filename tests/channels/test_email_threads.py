from durin.channels.email_threads import (
    decode_thread_index_conv_id,
    ensure_angle_brackets,
    normalize_subject,
    thread_digest,
)


def test_ensure_angle_brackets_repairs_and_passes_through():
    assert ensure_angle_brackets("m1@example.com") == "<m1@example.com>"
    assert ensure_angle_brackets("<m1@example.com>") == "<m1@example.com>"
    assert ensure_angle_brackets("&lt;m1@example.com&gt;") == "<m1@example.com>"
    assert ensure_angle_brackets("  <m1@example.com>  ") == "<m1@example.com>"
    assert ensure_angle_brackets("") == ""


def test_normalize_subject_strips_prefixes_multilingual():
    assert normalize_subject("Re: Invoice #42") == "invoice #42"
    assert normalize_subject("RE: RE: Invoice #42") == "invoice #42"
    assert normalize_subject("Re[2]: Invoice #42") == "invoice #42"
    assert normalize_subject("AW: Rechnung") == "rechnung"
    assert normalize_subject("[EXT] Fwd: hello  world") == "hello world"
    assert normalize_subject("回复: 你好") == "你好"
    assert normalize_subject("Plain subject") == "plain subject"


def test_normalize_subject_does_not_eat_words_starting_with_re():
    assert normalize_subject("Recreation plans") == "recreation plans"


def test_decode_thread_index_conv_id():
    import base64

    raw = bytes(range(22)) + b"\x01\x02\x03\x04\x05"  # 22-byte prefix + one child block
    header = base64.b64encode(raw).decode()
    assert decode_thread_index_conv_id(header) == bytes(range(22)).hex()
    # Child blocks do not change the conversation id.
    header2 = base64.b64encode(bytes(range(22)) + b"\xff\xff\xff\xff\xff").decode()
    assert decode_thread_index_conv_id(header2) == decode_thread_index_conv_id(header)
    assert decode_thread_index_conv_id("") == ""
    assert decode_thread_index_conv_id("not base64!!!") == ""
    assert decode_thread_index_conv_id(base64.b64encode(b"short").decode()) == ""


def test_thread_digest_stable_and_bracket_insensitive():
    d1 = thread_digest("<m1@example.com>")
    d2 = thread_digest("m1@example.com")
    assert d1 == d2
    assert len(d1) == 16
    assert d1 != thread_digest("<m2@example.com>")


import json
import time

from durin.channels.email_threads import ThreadStore


def _mk_store(tmp_path, **kw) -> ThreadStore:
    store = ThreadStore(tmp_path / "threads.json", **kw)
    store.load()
    return store


def test_store_upsert_get_and_persistence(tmp_path):
    store = _mk_store(tmp_path)
    store.upsert_inbound(
        "d1", root="<m1@x>", address="alice@x", subject="Invoice #42",
        references=["<m0@x>"], message_id="<m1@x>",
        thread_index_conv_id="aabb", thread_topic="Invoice #42",
    )
    entry = store.get("d1")
    assert entry["references"] == ["<m0@x>", "<m1@x>"]
    assert entry["last_message_id"] == "<m1@x>"
    # Reload from disk — survives restart.
    store2 = _mk_store(tmp_path)
    assert store2.get("d1")["subject"] == "Invoice #42"


def test_store_record_outbound_extends_chain(tmp_path):
    store = _mk_store(tmp_path)
    store.upsert_inbound("d1", root="<m1@x>", address="alice@x",
                         subject="s", references=[], message_id="<m1@x>")
    store.record_outbound("d1", "<durin1@x>")
    entry = store.get("d1")
    assert entry["last_message_id"] == "<durin1@x>"
    assert entry["references"] == ["<m1@x>", "<durin1@x>"]


def test_store_latest_for_address(tmp_path):
    store = _mk_store(tmp_path)
    store.upsert_inbound("d1", root="<m1@x>", address="alice@x",
                         subject="old", references=[], message_id="<m1@x>")
    store._threads["d1"]["last_seen"] = time.time() - 100  # age the first one
    store.upsert_inbound("d2", root="<m2@x>", address="alice@x",
                         subject="new", references=[], message_id="<m2@x>")
    assert store.latest_for_address("alice@x")["subject"] == "new"
    assert store.latest_for_address("nobody@x") is None


def test_store_conv_index_lookup(tmp_path):
    store = _mk_store(tmp_path)
    store.upsert_inbound("d1", root="<m1@x>", address="a@x", subject="Re: Hello",
                         references=[], message_id="<m1@x>",
                         thread_index_conv_id="ffee")
    assert store.lookup_conv("ffee", "hello") == "d1"
    assert store.lookup_conv("ffee", "other subject") is None
    assert store.lookup_conv("0000", "hello") is None


def test_store_prunes_by_age_and_cap(tmp_path):
    store = _mk_store(tmp_path, max_age_days=30, max_entries=3)
    store.upsert_inbound("old", root="<o@x>", address="a@x", subject="s",
                         references=[], message_id="<o@x>")
    store._threads["old"]["last_seen"] = time.time() - 40 * 86400
    for i in range(4):
        store.upsert_inbound(f"d{i}", root=f"<m{i}@x>", address="a@x",
                             subject="s", references=[], message_id=f"<m{i}@x>")
        store._threads[f"d{i}"]["last_seen"] = time.time() - (10 - i)
    store.prune()
    assert store.get("old") is None          # age-pruned
    assert len(store._threads) == 3          # cap enforced, was 4
    assert store.get("d0") is None           # oldest dropped
    assert store.get("d3") is not None       # newest kept


def test_store_references_chain_is_capped(tmp_path):
    store = _mk_store(tmp_path)
    refs = [f"<r{i}@x>" for i in range(40)]
    store.upsert_inbound("d1", root="<r0@x>", address="a@x", subject="s",
                         references=refs, message_id="<m@x>")
    chain = store.get("d1")["references"]
    assert chain[0] == "<r0@x>"              # thread identity kept
    assert chain[-1] == "<m@x>"              # newest kept
    assert len(chain) == 21                  # first + last 20


def test_store_corrupt_file_starts_empty(tmp_path):
    path = tmp_path / "threads.json"
    path.write_text("{not json")
    store = ThreadStore(path)
    store.load()
    assert store._threads == {}
    # And it can write again afterwards.
    store.upsert_inbound("d1", root="<m@x>", address="a@x", subject="s",
                         references=[], message_id="<m@x>")
    assert json.loads(path.read_text())["d1"]["root"] == "<m@x>"
