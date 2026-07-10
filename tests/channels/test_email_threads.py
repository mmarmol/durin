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
