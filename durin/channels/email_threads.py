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


_REFS_KEEP_TAIL = 20  # stored chain = first (thread identity) + last N


class ThreadStore:
    """Per-thread reply state for the email channel.

    One JSON file, one writer (the channel inside the gateway process),
    atomic writes. Source of truth for header stitching only — conversation
    content lives in durin sessions, so losing this file degrades replies to
    "no In-Reply-To", never loses conversation.
    """

    def __init__(self, path: Path, *, max_age_days: int = 30, max_entries: int = 5000):
        self._path = path
        self._max_age_seconds = max_age_days * 86400
        self._max_entries = max_entries
        self._threads: dict[str, dict[str, Any]] = {}
        # (conv_id, normalized subject) -> digest. Rebuilt on load.
        self._conv_index: dict[tuple[str, str], str] = {}

    def load(self) -> None:
        self._threads = {}
        try:
            if self._path.exists():
                data = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._threads = {
                        k: v for k, v in data.items() if isinstance(v, dict)
                    }
        except Exception as exc:
            logger.warning("Email thread store unreadable, starting empty: {}", exc)
            self._threads = {}
        self.prune()
        self._rebuild_conv_index()

    def get(self, digest: str) -> dict[str, Any] | None:
        return self._threads.get(digest)

    def latest_for_address(self, address: str) -> dict[str, Any] | None:
        needle = address.lower()
        candidates = [
            e for e in self._threads.values() if e.get("address", "").lower() == needle
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda e: e.get("last_seen", 0.0))

    def lookup_conv(self, conv_id: str, norm_subject: str) -> str | None:
        if not conv_id:
            return None
        digest = self._conv_index.get((conv_id, norm_subject))
        # The index may point at an entry that was pruned since indexing.
        return digest if digest in self._threads else None

    def upsert_inbound(
        self,
        digest: str,
        *,
        root: str,
        address: str,
        subject: str,
        references: list[str],
        message_id: str,
        thread_index_conv_id: str = "",
        thread_topic: str = "",
    ) -> None:
        chain = [r for r in references if r]
        if message_id and message_id not in chain:
            chain.append(message_id)
        entry = self._threads.get(digest) or {}
        entry.update(
            root=root,
            address=address,
            subject=subject or entry.get("subject", ""),
            references=self._cap_chain(chain),
            last_message_id=message_id or entry.get("last_message_id", ""),
            thread_index_conv_id=thread_index_conv_id or entry.get("thread_index_conv_id", ""),
            thread_topic=thread_topic or entry.get("thread_topic", ""),
            last_seen=time.time(),
        )
        self._threads[digest] = entry
        if entry["thread_index_conv_id"]:
            key = (entry["thread_index_conv_id"], normalize_subject(entry["subject"]))
            self._conv_index[key] = digest
        self._save()

    def record_outbound(self, digest: str, own_message_id: str) -> None:
        entry = self._threads.get(digest)
        if entry is None:
            return
        chain = list(entry.get("references") or [])
        if own_message_id and own_message_id not in chain:
            chain.append(own_message_id)
        entry["references"] = self._cap_chain(chain)
        entry["last_message_id"] = own_message_id
        entry["last_seen"] = time.time()
        self._save()

    def prune(self) -> None:
        before = len(self._threads)
        cutoff = time.time() - self._max_age_seconds
        self._threads = {
            k: v for k, v in self._threads.items()
            if v.get("last_seen", 0.0) >= cutoff
        }
        if len(self._threads) > self._max_entries:
            by_age = sorted(
                self._threads.items(), key=lambda kv: kv[1].get("last_seen", 0.0)
            )
            self._threads = dict(by_age[len(by_age) - self._max_entries:])
        self._rebuild_conv_index()
        if len(self._threads) != before:
            self._save()

    @staticmethod
    def _cap_chain(chain: list[str]) -> list[str]:
        if len(chain) <= 1 + _REFS_KEEP_TAIL:
            return chain
        return [chain[0]] + chain[-_REFS_KEEP_TAIL:]

    def _rebuild_conv_index(self) -> None:
        self._conv_index = {}
        for digest, entry in self._threads.items():
            conv = entry.get("thread_index_conv_id") or ""
            if conv:
                self._conv_index[(conv, normalize_subject(entry.get("subject", "")))] = digest

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self._threads, ensure_ascii=False), encoding="utf-8"
            )
            tmp.replace(self._path)
        except Exception as exc:
            logger.warning("Email thread store write failed: {}", exc)
