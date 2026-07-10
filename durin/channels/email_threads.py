"""Thread identity helpers and the persistent thread store for the email channel.

Email carries its own conversation identity: every mail has a Message-ID,
replies carry In-Reply-To and the References chain. The first Message-ID in
References is a stable thread identifier ("thread root"). Outlook/Exchange
sometimes rewrites References on internal hops, so a secondary index keyed by
the Thread-Index conversation prefix + normalized subject recovers those
threads.
"""

import base64
import hashlib
import html as html_mod
import json
import re
import time
from pathlib import Path
from typing import Any

from loguru import logger

# Localized reply/forward prefixes (EN, DE, FR, IT, ES, PT, NL, NO/DA, SE,
# FI, PL + CJK), numbered variants like "Re[2]:", bracketed tags "[EXT]".
_REPLY_PREFIX_RE = re.compile(
    r"^\s*(?:"
    r"\[(?:ext|external|extern)\]|"
    r"(?:re|aw|fwd?|wg|tr|sv|vs|rif|r|rv|ant|vl|odp|pd"
    r"|回复|答复|转发|回覆|轉寄|返信|転送|회신|답장|전달)"
    r"(?:\[\d+\])?\s*[:：]"
    r")\s*",
    re.IGNORECASE,
)


def ensure_angle_brackets(value: str) -> str:
    """Normalize a Message-ID to canonical ``<id>`` form.

    IDs read back from config/LLM paths sometimes lose their angle brackets
    or arrive HTML-escaped; headers built from a bare id break threading in
    strict clients.
    """
    v = html_mod.unescape((value or "").strip())
    if not v:
        return ""
    if not v.startswith("<"):
        v = "<" + v
    if not v.endswith(">"):
        v = v + ">"
    return v


def normalize_subject(value: str) -> str:
    """Strip reply/forward prefixes repeatedly, collapse whitespace, lowercase."""
    s = (value or "").strip()
    while True:
        nxt = _REPLY_PREFIX_RE.sub("", s, count=1)
        if nxt == s:
            break
        s = nxt
    return " ".join(s.split()).lower()


def decode_thread_index_conv_id(value: str) -> str:
    """Hex conversation prefix (22 bytes) of an Outlook Thread-Index header.

    The remainder (5-byte child blocks per reply) identifies the position in
    the thread, not the conversation, so it is discarded. Returns "" when the
    header is missing or malformed.
    """
    v = (value or "").strip()
    if not v:
        return ""
    try:
        raw = base64.b64decode(v, validate=True)
    except Exception:
        return ""
    if len(raw) < 22:
        return ""
    return raw[:22].hex()


def thread_digest(root: str) -> str:
    """Short stable id for a thread root — 16 hex chars of SHA-256."""
    canonical = ensure_angle_brackets(root)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
