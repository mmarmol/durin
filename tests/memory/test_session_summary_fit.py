"""The session summary a prompt carries, cut to the tokens it may take."""

from __future__ import annotations

from durin.memory.session_summary_store import fit_summary_to_tokens
from durin.utils.helpers import estimate_text_tokens

_SEP = "\n\n---\n"
_HEAD = "=== ARCHIVED SUMMARY (consolidator) ==="
_END = "=== END ARCHIVED SUMMARY ===\nThe turns summarized above are archived, not lost."


def _framed(blocks: list[str]) -> str:
    return f"{_HEAD}\n{_SEP.join(blocks)}\n{_END}"


def _block(i: int) -> str:
    return f"- span {i}: " + "a fact worth keeping " * 40


def test_a_summary_that_fits_is_unchanged():
    text = _framed([_block(1), _block(2)])

    assert fit_summary_to_tokens(text, estimate_text_tokens(text)) == text


def test_the_oldest_blocks_are_left_out_and_the_frame_kept():
    blocks = [_block(i) for i in range(8)]
    text = _framed(blocks)
    limit = estimate_text_tokens(text) // 3

    fitted = fit_summary_to_tokens(text, limit)

    assert estimate_text_tokens(fitted) <= limit
    assert fitted.startswith(_HEAD + "\n")
    assert fitted.endswith(_END)
    assert "left out to fit the context window" in fitted
    assert "- span 7:" in fitted
    assert "- span 0:" not in fitted


def test_the_newest_blocks_last_lines_when_no_block_fits_whole():
    newest = "\n".join(f"- line {i}: " + "detail " * 20 for i in range(30))
    text = _framed([_block(0), newest])
    limit = estimate_text_tokens(newest) // 2

    fitted = fit_summary_to_tokens(text, limit)

    assert estimate_text_tokens(fitted) <= limit
    assert "- line 29:" in fitted
    assert "- line 0:" not in fitted


def test_nothing_when_not_even_the_frame_fits():
    assert fit_summary_to_tokens(_framed([_block(0)]), 10) == ""


def test_an_unframed_summary_is_cut_the_same_way():
    blocks = [_block(i) for i in range(6)]
    text = _SEP.join(blocks)
    limit = estimate_text_tokens(text) // 2

    fitted = fit_summary_to_tokens(text, limit)

    assert estimate_text_tokens(fitted) <= limit
    assert fitted.endswith(blocks[-1])


_CARRIED = "Files/paths from earlier spans (evicted): /srv/billing/values-prod.yaml; /etc/nginx/sites/api.conf"


def test_the_carried_paths_outlive_the_oldest_blocks():
    """The store keeps the paths of the blocks it evicted in a head block,
    so they survive the eviction. The cut left that block out first, being
    the oldest: 50 tokens under the whole summary, the paths were gone."""
    text = _framed([_CARRIED, *(_block(i) for i in range(6))])
    limit = estimate_text_tokens(text) - 50

    fitted = fit_summary_to_tokens(text, limit)

    assert estimate_text_tokens(fitted) <= limit
    assert "values-prod.yaml" in fitted
    assert "- span 0:" not in fitted
    assert "- span 5:" in fitted


def test_the_carried_paths_go_last():
    text = _framed([_CARRIED, *(_block(i) for i in range(6))])
    carried_only = _framed([_CARRIED])

    assert "values-prod.yaml" in fit_summary_to_tokens(text, estimate_text_tokens(carried_only) + 30)
    assert fit_summary_to_tokens(text, 10) == ""
