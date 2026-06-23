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


import pytest

from durin.voice.rendition import SpokenRendition, build_spoken_rendition


@pytest.mark.asyncio
async def test_short_answer_spoken_verbatim_cleaned():
    r = await build_spoken_rendition("Just a quick `foo()` answer.")
    assert isinstance(r, SpokenRendition)
    assert r.summarized is False
    assert r.spoken == "Just a quick foo() answer."
    assert r.displayed == "Just a quick `foo()` answer."  # unchanged


@pytest.mark.asyncio
async def test_verbatim_mode_speaks_full_even_when_long():
    long_text = " ".join(["word"] * 100)
    r = await build_spoken_rendition(long_text, mode="verbatim", long_threshold_words=60)
    assert r.summarized is False
    assert r.spoken == long_text


@pytest.mark.asyncio
async def test_model_led_takes_first_paragraph_and_appends_pointer():
    text = "Short spoken lead sentence here.\n\n" + " ".join(["detail"] * 80)
    r = await build_spoken_rendition(text, mode="model_led", long_threshold_words=60)
    assert r.summarized is True
    assert r.lead_present is True
    assert r.spoken.startswith("Short spoken lead sentence here.")
    assert r.spoken.endswith("The full answer is on screen.")
    assert "detail detail" not in r.spoken  # body not spoken


@pytest.mark.asyncio
async def test_model_led_degrades_when_lead_is_a_described_block():
    # Starts with a big code block (no prose lead) → still safe, never speaks code.
    text = "```\n" + "\n".join(["line"] * 80) + "\n```\n\nthen prose"
    r = await build_spoken_rendition(text, mode="model_led", long_threshold_words=2)
    assert "line\nline" not in r.spoken
    assert r.spoken.endswith("The full answer is on screen.")
