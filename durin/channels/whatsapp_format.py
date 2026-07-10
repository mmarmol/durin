"""Markdown → WhatsApp formatting and message chunking (pure functions)."""

import re

# WhatsApp renders far more than this, but long walls read badly on phones;
# 4096 matches the practical limit the other channels use.
WHATSAPP_MAX_LEN = 4096

# Fenced blocks and inline code are extracted before conversion so their
# contents pass through untouched, then restored.
_CODE_RE = re.compile(r"```.*?```|`[^`\n]+`", re.DOTALL)
_BOLD_SENTINEL = "\x01"
_STASH_L = "\x00["
_STASH_R = "]\x00"


def markdown_to_whatsapp(text: str) -> str:
    """Convert standard markdown to WhatsApp's formatting dialect."""
    stash: list[str] = []

    def _stash(m: re.Match) -> str:
        stash.append(m.group(0))
        return f"{_STASH_L}{len(stash) - 1}{_STASH_R}"

    out = _CODE_RE.sub(_stash, text)
    out = re.sub(r"^#{1,6}\s+(.+)$", r"**\1**", out, flags=re.MULTILINE)
    # Bold first (via sentinel) so the single-star italic pass can't eat it.
    out = re.sub(r"\*\*(.+?)\*\*", rf"{_BOLD_SENTINEL}\1{_BOLD_SENTINEL}", out)
    out = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"_\1_", out)
    out = out.replace(_BOLD_SENTINEL, "*")
    out = re.sub(r"~~(.+?)~~", r"~\1~", out)
    out = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"\1 (\2)", out)
    for i, code in enumerate(stash):
        out = out.replace(f"{_STASH_L}{i}{_STASH_R}", code)
    return out


def chunk_message(text: str, limit: int = WHATSAPP_MAX_LEN) -> list[str]:
    """Split ``text`` into <=limit chunks, preferring paragraph then line
    boundaries, keeping code fences balanced per chunk (close + reopen)."""
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    reopen_fence = False
    while remaining:
        prefix = "```\n" if reopen_fence else ""
        # Reserve room for the prefix and a possible closing fence.
        budget = limit - len(prefix) - 4
        if len(remaining) <= budget:
            piece, remaining = remaining, ""
        else:
            window = remaining[:budget]
            cut = window.rfind("\n\n")
            if cut < budget // 2:
                cut = window.rfind("\n")
            if cut < budget // 2:
                cut = budget
            piece, remaining = remaining[:cut], remaining[cut:].lstrip("\n")
        fence_open = (prefix + piece).count("```") % 2 == 1
        suffix = "\n```" if fence_open and remaining else ""
        chunks.append(prefix + piece + suffix)
        reopen_fence = fence_open and bool(remaining)
    return chunks
