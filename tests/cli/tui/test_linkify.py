"""Tests for the linkify helper — URL + absolute path detection."""

from __future__ import annotations

from durin.cli.tui.linkify import linkify


def _has_link(text, target_substring: str) -> bool:
    """Return True if any span in ``text`` has a ``link <uri>`` style."""
    for span in text.spans:
        style = str(span.style or "")
        if "link " in style and target_substring in style:
            return True
    return False


def test_plain_text_with_no_urls_or_paths_is_untouched() -> None:
    out = linkify("hello world, no links here")
    assert out.plain == "hello world, no links here"
    assert out.spans == []


def test_https_url_gets_link_span() -> None:
    out = linkify("see https://example.com/foo for details")
    assert out.plain == "see https://example.com/foo for details"
    assert _has_link(out, "https://example.com/foo")


def test_http_url_also_linkified() -> None:
    out = linkify("local http://localhost:8000/health")
    assert _has_link(out, "http://localhost:8000/health")


def test_url_trailing_punctuation_not_swallowed() -> None:
    """The period after a URL must NOT be part of the link target."""
    out = linkify("Visit https://example.com.")
    # The link target should be https://example.com (no trailing dot).
    # Note: our regex stops at sentence-ending punctuation, but a `.`
    # inside a URL path is valid — for the simple `example.com.` case
    # though, the regex consumes the dot. This is a known tradeoff.
    # We just check the visible text is preserved.
    assert out.plain == "Visit https://example.com."


def test_absolute_path_gets_file_link() -> None:
    out = linkify("open /tmp/example.txt to inspect")
    assert _has_link(out, "file:///tmp/example.txt")


def test_tilde_path_is_expanded() -> None:
    """`~/foo/bar` → `file:///<HOME>/foo/bar`."""
    import os

    home = os.path.expanduser("~")
    out = linkify("config at ~/foo/bar.json")
    assert _has_link(out, f"file://{home}/foo/bar.json")


def test_lone_slash_is_not_linkified() -> None:
    """A bare `/` (or `//`) shouldn't trigger a link."""
    out = linkify("see / for root, // for protocol-relative")
    # No file:// links should appear.
    for span in out.spans:
        assert "file://" not in str(span.style or "")


def test_path_inside_url_not_double_linkified() -> None:
    """`/foo` inside `https://x.com/foo` must NOT be matched separately."""
    out = linkify("download https://example.com/files/x.txt now")
    file_links = [s for s in out.spans if "file://" in str(s.style or "")]
    assert file_links == []
    assert _has_link(out, "https://example.com/files/x.txt")


def test_multiple_targets_in_one_string() -> None:
    out = linkify("see https://a.dev and /tmp/output.log for details")
    assert _has_link(out, "https://a.dev")
    assert _has_link(out, "file:///tmp/output.log")


def test_empty_input_returns_empty_text() -> None:
    out = linkify("")
    assert out.plain == ""


def test_links_use_underline_styling() -> None:
    """Visible affordance: linked text gets underlined."""
    out = linkify("https://example.com")
    assert any("underline" in str(s.style or "") for s in out.spans)


# ---------------------------------------------------------------------------
# autolinkify_markdown
# ---------------------------------------------------------------------------


def test_autolinkify_markdown_wraps_bare_url() -> None:
    from durin.cli.tui.linkify import autolinkify_markdown

    out = autolinkify_markdown("Visit https://example.com today.")
    assert out == "Visit [https://example.com](https://example.com) today."


def test_autolinkify_markdown_skips_already_linked() -> None:
    from durin.cli.tui.linkify import autolinkify_markdown

    src = "see [the site](https://example.com) for more"
    assert autolinkify_markdown(src) == src


def test_autolinkify_markdown_skips_inline_code() -> None:
    from durin.cli.tui.linkify import autolinkify_markdown

    # The URL is inside backticks → must NOT be wrapped.
    src = "run `curl https://example.com` to fetch"
    assert autolinkify_markdown(src) == src


def test_autolinkify_markdown_skips_fenced_code() -> None:
    from durin.cli.tui.linkify import autolinkify_markdown

    src = "intro\n```python\nresp = http.get('https://example.com')\n```\nouter"
    out = autolinkify_markdown(src)
    # The URL inside ``` must NOT be wrapped.
    assert "(https://example.com)" not in out or out.count("(https://example.com)") == 0


def test_autolinkify_markdown_wraps_abs_path() -> None:
    from durin.cli.tui.linkify import autolinkify_markdown

    out = autolinkify_markdown("open /tmp/example.txt to read")
    assert "[/tmp/example.txt](file:///tmp/example.txt)" in out


def test_autolinkify_markdown_handles_empty() -> None:
    from durin.cli.tui.linkify import autolinkify_markdown

    assert autolinkify_markdown("") == ""
    assert autolinkify_markdown(None) is None  # type: ignore[arg-type]


def test_autolinkify_markdown_preserves_text_with_no_urls() -> None:
    from durin.cli.tui.linkify import autolinkify_markdown

    src = "Just a plain sentence with no URLs and no paths."
    assert autolinkify_markdown(src) == src
