from durin.voice.rendition import speakable_transform


def test_transform_describes_code_block():
    out = speakable_transform("Here:\n```python\nprint(1)\n```\nDone")
    assert "print(1)" not in out
    assert "the code is on screen" in out
    assert "Here" in out and "Done" in out


def test_transform_strips_inline_code_backticks():
    assert speakable_transform("call `foo()` now") == "call foo() now"


def test_transform_describes_bare_url():
    out = speakable_transform("see https://example.com/x?y=1 for more")
    assert "https://" not in out
    assert "a link" in out


def test_transform_keeps_link_text_drops_url():
    assert speakable_transform("see [the docs](https://x.com)") == "see the docs"


def test_transform_describes_table():
    out = speakable_transform("| A | B |\n|---|---|\n| 1 | 2 |")
    assert "a table" in out
    assert "---" not in out


def test_transform_strips_headings_and_emphasis():
    assert speakable_transform("# Title\nthis is **bold** text") == (
        "Title\nthis is bold text"
    )


def test_transform_localized_labels():
    from durin.voice.rendition import SpeakableLabels

    labels = SpeakableLabels(code_block="código en pantalla")
    out = speakable_transform("```\nx\n```", labels=labels)
    assert "código en pantalla" in out
