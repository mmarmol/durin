"""Tests for markdown→WhatsApp conversion and message chunking."""

from durin.channels.whatsapp_format import (
    WHATSAPP_MAX_LEN,
    chunk_message,
    markdown_to_whatsapp,
)


class TestMarkdownToWhatsApp:
    def test_bold(self):
        assert markdown_to_whatsapp("**hola**") == "*hola*"
        assert markdown_to_whatsapp("__hola__") == "__hola__"

    def test_dunder_identifiers_preserved(self):
        text = "the __init__ method calls __repr__"
        assert markdown_to_whatsapp(text) == text

    def test_italic_single_star_becomes_underscore(self):
        assert markdown_to_whatsapp("*hola*") == "_hola_"

    def test_bold_not_mangled_by_italic_pass(self):
        assert markdown_to_whatsapp("**a** y *b*") == "*a* y _b_"

    def test_strikethrough(self):
        assert markdown_to_whatsapp("~~fuera~~") == "~fuera~"

    def test_headers_become_bold_lines(self):
        assert markdown_to_whatsapp("## Titulo\ncuerpo") == "*Titulo*\ncuerpo"

    def test_links_flattened(self):
        assert markdown_to_whatsapp("[docs](https://x.io/d)") == "docs (https://x.io/d)"

    def test_inline_code_protected(self):
        assert markdown_to_whatsapp("usa `**raw**` ok") == "usa `**raw**` ok"

    def test_fenced_code_protected(self):
        block = "```py\n**not bold** [no](http://link)\n```"
        assert markdown_to_whatsapp(block) == block

    def test_plain_text_unchanged(self):
        assert markdown_to_whatsapp("hola 2*3=6 :)") == "hola 2*3=6 :)"


class TestChunkMessage:
    def test_short_message_single_chunk(self):
        assert chunk_message("hola") == ["hola"]

    def test_empty_returns_empty_list(self):
        assert chunk_message("") == []

    def test_respects_limit(self):
        text = "palabra " * 2000  # ~16k chars
        chunks = chunk_message(text)
        assert len(chunks) > 1
        assert all(len(c) <= WHATSAPP_MAX_LEN for c in chunks)

    def test_prefers_paragraph_boundaries(self):
        para = "x" * 3000
        text = f"{para}\n\n{para}"
        chunks = chunk_message(text)
        assert chunks[0] == para
        assert chunks[1] == para

    def test_reassembly_loses_no_content(self):
        text = ("linea con contenido util\n" * 500).strip()
        joined = "\n".join(chunk_message(text))
        assert joined.replace("\n", "") == text.replace("\n", "")

    def test_code_fence_closed_and_reopened(self):
        text = "```\n" + ("codigo\n" * 900) + "```"
        chunks = chunk_message(text)
        assert len(chunks) > 1
        for c in chunks:
            assert c.count("```") % 2 == 0, f"unbalanced fence in chunk: {c[:60]}..."
