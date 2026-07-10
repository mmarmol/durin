"""Email channel implementation using IMAP polling + SMTP replies."""

import asyncio
import base64
import html
import imaplib
import re
import smtplib
import ssl
import time
from contextlib import suppress
from datetime import date
from email import policy
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import make_msgid, parseaddr
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Literal

from loguru import logger
from pydantic import Field

from durin.bus.events import OutboundMessage
from durin.bus.queue import MessageBus
from durin.channels.base import BaseChannel
from durin.channels.email_threads import (
    ThreadStore,
    decode_thread_index_conv_id,
    ensure_angle_brackets,
    normalize_subject,
    thread_digest,
)
from durin.config.paths import get_media_dir, get_runtime_subdir
from durin.config.schema import Base
from durin.utils.helpers import safe_filename


class EmailConfig(Base):
    """Email channel configuration (IMAP inbound + SMTP outbound)."""

    enabled: bool = False
    consent_granted: bool = Field(default=False, json_schema_extra={"group": "access"})
    # Default persona for sessions born on this channel (empty = global default).
    persona: str = Field(default="", json_schema_extra={"group": "behavior"})

    imap_host: str = Field(default="", json_schema_extra={"group": "imap", "required": True})
    imap_port: int = Field(default=993, json_schema_extra={"group": "imap"})
    imap_username: str = Field(default="", json_schema_extra={"group": "imap", "required": True})
    imap_password: str = Field(default="", json_schema_extra={"group": "imap", "secret": True})
    imap_mailbox: str = Field(default="INBOX", json_schema_extra={"group": "imap"})
    imap_use_ssl: bool = Field(default=True, json_schema_extra={"group": "imap"})

    smtp_host: str = Field(default="", json_schema_extra={"group": "smtp", "required": True})
    smtp_port: int = Field(default=587, json_schema_extra={"group": "smtp"})
    smtp_username: str = Field(default="", json_schema_extra={"group": "smtp", "required": True})
    smtp_password: str = Field(default="", json_schema_extra={"group": "smtp", "secret": True})
    smtp_use_tls: bool = Field(default=True, json_schema_extra={"group": "smtp"})
    smtp_use_ssl: bool = Field(default=False, json_schema_extra={"group": "smtp"})
    from_address: str = Field(default="", json_schema_extra={"group": "smtp"})

    auto_reply_enabled: bool = Field(default=True, json_schema_extra={"group": "behavior"})
    poll_interval_seconds: int = Field(default=30, json_schema_extra={"group": "behavior"})
    mark_seen: bool = Field(default=True, json_schema_extra={"group": "behavior"})
    max_body_chars: int = Field(default=12000, json_schema_extra={"group": "behavior"})
    subject_prefix: str = Field(default="Re: ", json_schema_extra={"group": "behavior"})
    threading_mode: Literal["thread", "sender"] = Field(
        default="thread", json_schema_extra={"group": "behavior"}
    )
    allow_from: list[str] = Field(default_factory=list, json_schema_extra={"group": "access"})

    # Email authentication verification (anti-spoofing)
    verify_dkim: bool = Field(default=True, json_schema_extra={"group": "security"})
    verify_spf: bool = Field(default=True, json_schema_extra={"group": "security"})

    # Attachment handling — set allowed types to enable (e.g. ["application/pdf", "image/*"], or ["*"] for all)
    allowed_attachment_types: list[str] = Field(default_factory=list, json_schema_extra={"group": "attachments"})
    max_attachment_size: int = Field(default=2_000_000, json_schema_extra={"group": "attachments"})
    max_attachments_per_email: int = Field(default=5, json_schema_extra={"group": "attachments"})


class EmailChannel(BaseChannel):
    """
    Email channel.

    Inbound:
    - Poll IMAP mailbox for unread messages.
    - Convert each message into an inbound event.

    Outbound:
    - Send responses via SMTP back to the sender address.
    """

    name = "email"
    display_name = "Email"
    channel_description = "Receive and reply to email via IMAP (inbound) and SMTP (outbound)."
    _IMAP_MONTHS = (
        "Jan",
        "Feb",
        "Mar",
        "Apr",
        "May",
        "Jun",
        "Jul",
        "Aug",
        "Sep",
        "Oct",
        "Nov",
        "Dec",
    )
    _IMAP_RECONNECT_MARKERS = (
        "disconnected for inactivity",
        "eof occurred in violation of protocol",
        "socket error",
        "connection reset",
        "broken pipe",
        "bye",
    )
    _IMAP_MISSING_MAILBOX_MARKERS = (
        "mailbox doesn't exist",
        "select failed",
        "no such mailbox",
        "can't open mailbox",
        "does not exist",
    )

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return EmailConfig().model_dump(by_alias=False)

    @classmethod
    def config_model(cls) -> type | None:
        return EmailConfig

    def __init__(self, config: Any, bus: MessageBus):
        if isinstance(config, dict):
            config = EmailConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: EmailConfig = config
        self._self_addresses = self._collect_self_addresses()
        self._processed_uids: set[str] = set()  # Capped to prevent unbounded growth
        self._MAX_PROCESSED_UIDS = 100000
        self._store = ThreadStore(get_runtime_subdir("email") / "threads.json")
        self._store.load()
        self._last_prune = time.time()
        self._sent_folder: str | None = None

    async def start(self) -> None:
        """Start polling IMAP for inbound emails."""
        if not self.config.consent_granted:
            self.logger.warning(
                "Email channel disabled: consent_granted is false. "
                "Set channels.email.consentGranted=true after explicit user permission."
            )
            return

        if not self._validate_config():
            return

        self._running = True
        self._store.load()
        if not self.config.verify_dkim and not self.config.verify_spf:
            self.logger.warning(
                "DKIM and SPF verification are both DISABLED. "
                "Emails with spoofed From headers will be accepted. "
                "Set verify_dkim=true and verify_spf=true for anti-spoofing protection."
            )
        self.logger.info("Starting Email channel (IMAP polling mode)...")

        poll_seconds = max(5, int(self.config.poll_interval_seconds))
        while self._running:
            try:
                inbound_items = await asyncio.to_thread(self._fetch_new_messages)
                for item in inbound_items:
                    sender = item["sender"]

                    digest = self._resolve_thread(item)
                    session_key = (
                        f"email:{sender}:{digest}"
                        if self.config.threading_mode == "thread"
                        else None
                    )
                    await self._handle_message(
                        sender_id=sender,
                        chat_id=sender,
                        content=item["content"],
                        media=item.get("media") or None,
                        metadata=item.get("metadata", {}),
                        session_key=session_key,
                    )
            except Exception:
                self.logger.exception("Polling error")

            await asyncio.sleep(poll_seconds)
            if time.time() - self._last_prune > 86400:
                self._store.prune()
                self._last_prune = time.time()

    async def stop(self) -> None:
        """Stop polling loop."""
        self._running = False

    async def send(self, msg: OutboundMessage) -> None:
        """Send email via SMTP."""
        if not self.config.consent_granted:
            self.logger.warning("Skip email send: consent_granted is false")
            return

        if not self.config.smtp_host:
            self.logger.warning("SMTP host not configured")
            return

        to_addr = msg.chat_id.strip()
        if not to_addr:
            self.logger.warning("Missing recipient address")
            return

        # Resolve thread state from the store: explicit digest from outbound
        # metadata (set by the agent loop from the session key) → latest
        # thread for the address → fresh mail with no reply context.
        thread_meta = (msg.metadata or {}).get("email") or {}
        entry = None
        digest = ""
        if isinstance(thread_meta, dict) and thread_meta.get("thread"):
            entry = self._store.get(str(thread_meta["thread"]))
            digest = str(thread_meta["thread"])
        if entry is None:
            entry = self._store.latest_for_address(to_addr)
            digest = thread_digest(entry["root"]) if entry else ""
        is_reply = entry is not None
        force_send = bool((msg.metadata or {}).get("force_send"))

        # autoReplyEnabled only controls automatic replies, not proactive sends
        if is_reply and not self.config.auto_reply_enabled and not force_send:
            self.logger.info("Skip automatic reply to {}: auto_reply_enabled is false", to_addr)
            return

        base_subject = (entry or {}).get("subject") or "durin reply"
        subject = self._reply_subject(base_subject)
        if msg.metadata and isinstance(msg.metadata.get("subject"), str):
            override = msg.metadata["subject"].strip()
            if override:
                subject = override

        email_msg = EmailMessage()
        email_msg["From"] = self.config.from_address or self.config.smtp_username or self.config.imap_username
        email_msg["To"] = to_addr
        email_msg["Subject"] = subject

        body_md = msg.content or ""
        email_msg.set_content(body_md)
        try:
            from markdown_it import MarkdownIt

            html_body = MarkdownIt("commonmark").enable("table").render(body_md)
            email_msg.add_alternative(html_body, subtype="html")
        except Exception as exc:
            self.logger.warning("Markdown→HTML render failed, sending plain only: {}", exc)

        from_addr = parseaddr(email_msg["From"] or "")[1] or "durin@localhost"
        own_message_id = make_msgid(domain=from_addr.split("@")[-1])
        email_msg["Message-ID"] = own_message_id
        if entry:
            chain = [ensure_angle_brackets(r) for r in entry.get("references") or []]
            last_id = ensure_angle_brackets(entry.get("last_message_id", "")) or (
                chain[-1] if chain else ""
            )
            if last_id:
                email_msg["In-Reply-To"] = last_id
                if last_id not in chain:
                    chain.append(last_id)  # invariant: References ends with In-Reply-To
                email_msg["References"] = " ".join(chain)
            if entry.get("thread_index_conv_id"):
                try:
                    email_msg["Thread-Index"] = base64.b64encode(
                        bytes.fromhex(entry["thread_index_conv_id"])
                    ).decode()
                except Exception as exc:
                    self.logger.warning(
                        "Skipping Thread-Index re-emit: corrupt thread_index_conv_id {!r}: {}",
                        entry["thread_index_conv_id"], exc,
                    )
            if entry.get("thread_topic"):
                email_msg["Thread-Topic"] = entry["thread_topic"]

        try:
            await asyncio.to_thread(self._smtp_send, email_msg)
        except Exception:
            self.logger.exception("Error sending to {}", to_addr)
            raise

        await asyncio.to_thread(self._append_to_sent, email_msg)

        if entry and digest:
            self._store.record_outbound(digest, own_message_id)

    def _validate_config(self) -> bool:
        missing = []
        if not self.config.imap_host:
            missing.append("imap_host")
        if not self.config.imap_username:
            missing.append("imap_username")
        if not self.config.imap_password:
            missing.append("imap_password")
        if not self.config.smtp_host:
            missing.append("smtp_host")
        if not self.config.smtp_username:
            missing.append("smtp_username")
        if not self.config.smtp_password:
            missing.append("smtp_password")

        if missing:
            self.logger.error("Channel not configured, missing: {}", ', '.join(missing))
            return False
        return True

    def _smtp_send(self, msg: EmailMessage) -> None:
        timeout = 30
        if self.config.smtp_use_ssl:
            with smtplib.SMTP_SSL(
                self.config.smtp_host,
                self.config.smtp_port,
                timeout=timeout,
            ) as smtp:
                smtp.login(self.config.smtp_username, self.config.smtp_password)
                smtp.send_message(msg)
            return

        with smtplib.SMTP(self.config.smtp_host, self.config.smtp_port, timeout=timeout) as smtp:
            if self.config.smtp_use_tls:
                smtp.starttls(context=ssl.create_default_context())
            smtp.login(self.config.smtp_username, self.config.smtp_password)
            smtp.send_message(msg)

    def _detect_sent_folder(self, client: Any) -> str | None:
        """Find the mailbox flagged \\Sent via LIST.

        Returns the folder name when a \\Sent-flagged mailbox was actually
        found, or None when LIST failed or nothing matched — callers must
        not cache None, so a transient failure gets retried on the next send.
        """
        try:
            status, boxes = client.list()
            if status == "OK":
                for line in boxes or []:
                    decoded = line.decode("utf-8", errors="ignore") if isinstance(line, bytes) else str(line)
                    if "\\sent" in decoded.lower():
                        m = re.search(r'"([^"]+)"\s*$', decoded)
                        if m:
                            return f'"{m.group(1)}"'
                        # RFC 3501 LIST may carry unquoted mailbox atoms, e.g.
                        # (\HasNoChildren \Sent) "/" Sent — take the last token.
                        tokens = decoded.split()
                        if tokens:
                            return tokens[-1]
        except Exception as exc:
            self.logger.debug("Sent-folder detection failed: {}", exc)
        return None

    def _append_to_sent(self, email_msg: EmailMessage) -> None:
        """Best-effort copy of an outbound mail into the Sent folder.

        Without this, mail durin sends never appears in the mailbox: threads
        look one-sided from every mail client. Failure only logs — the SMTP
        send already succeeded.
        """
        try:
            if self.config.imap_use_ssl:
                client = imaplib.IMAP4_SSL(self.config.imap_host, self.config.imap_port)
            else:
                client = imaplib.IMAP4(self.config.imap_host, self.config.imap_port)
            try:
                client.login(self.config.imap_username, self.config.imap_password)
                detected = None
                if self._sent_folder is None:
                    detected = self._detect_sent_folder(client)
                    if detected is not None:
                        self._sent_folder = detected
                folder = self._sent_folder or detected or '"Sent"'
                client.append(
                    folder,
                    "(\\Seen)",
                    imaplib.Time2Internaldate(time.time()),
                    email_msg.as_bytes(),
                )
            finally:
                with suppress(Exception):
                    client.logout()
        except Exception as exc:
            self.logger.warning("Could not copy sent mail to Sent folder: {}", exc)

    def _fetch_new_messages(self) -> list[dict[str, Any]]:
        """Poll IMAP and return parsed unread messages."""
        return self._fetch_messages(
            search_criteria=("UNSEEN",),
            mark_seen=self.config.mark_seen,
            dedupe=True,
            limit=0,
        )

    def fetch_messages_between_dates(
        self,
        start_date: date,
        end_date: date,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """
        Fetch messages in [start_date, end_date) by IMAP date search.

        This is used for historical summarization tasks (e.g. "yesterday").
        """
        if end_date <= start_date:
            return []

        return self._fetch_messages(
            search_criteria=(
                "SINCE",
                self._format_imap_date(start_date),
                "BEFORE",
                self._format_imap_date(end_date),
            ),
            mark_seen=False,
            dedupe=False,
            limit=max(1, int(limit)),
        )

    def _fetch_messages(
        self,
        search_criteria: tuple[str, ...],
        mark_seen: bool,
        dedupe: bool,
        limit: int,
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        cycle_uids: set[str] = set()

        for attempt in range(2):
            try:
                self._fetch_messages_once(
                    search_criteria,
                    mark_seen,
                    dedupe,
                    limit,
                    messages,
                    cycle_uids,
                )
                return messages
            except Exception as exc:
                if attempt == 1 or not self._is_stale_imap_error(exc):
                    raise
                self.logger.warning("IMAP connection went stale, retrying once: {}", exc)

        return messages

    def _fetch_messages_once(
        self,
        search_criteria: tuple[str, ...],
        mark_seen: bool,
        dedupe: bool,
        limit: int,
        messages: list[dict[str, Any]],
        cycle_uids: set[str],
    ) -> None:
        """Fetch messages by arbitrary IMAP search criteria."""
        mailbox = self.config.imap_mailbox or "INBOX"

        if self.config.imap_use_ssl:
            client = imaplib.IMAP4_SSL(self.config.imap_host, self.config.imap_port)
        else:
            client = imaplib.IMAP4(self.config.imap_host, self.config.imap_port)

        try:
            client.login(self.config.imap_username, self.config.imap_password)
            try:
                status, _ = client.select(mailbox)
            except Exception as exc:
                if self._is_missing_mailbox_error(exc):
                    self.logger.warning("Mailbox unavailable, skipping poll for {}: {}", mailbox, exc)
                    return messages
                raise
            if status != "OK":
                self.logger.warning("Mailbox select returned {}, skipping poll for {}", status, mailbox)
                return messages

            status, data = client.search(None, *search_criteria)
            if status != "OK" or not data:
                return messages

            ids = data[0].split()
            if limit > 0 and len(ids) > limit:
                ids = ids[-limit:]
            for imap_id in ids:
                status, fetched = client.fetch(imap_id, "(BODY.PEEK[] UID)")
                if status != "OK" or not fetched:
                    continue

                raw_bytes = self._extract_message_bytes(fetched)
                if raw_bytes is None:
                    continue

                uid = self._extract_uid(fetched)
                if uid and uid in cycle_uids:
                    continue
                if dedupe and uid and uid in self._processed_uids:
                    continue

                parsed = BytesParser(policy=policy.default).parsebytes(raw_bytes)
                sender = parseaddr(parsed.get("From", ""))[1].strip().lower()
                if not sender:
                    continue
                if self._is_self_address(sender):
                    self.logger.info("From {} ignored: matches bot-owned address", sender)
                    self._remember_processed_uid(uid, dedupe, cycle_uids)
                    if mark_seen:
                        client.store(imap_id, "+FLAGS", "\\Seen")
                    continue

                auto_submitted = (parsed.get("Auto-Submitted", "") or "").strip().lower()
                if auto_submitted and auto_submitted != "no":
                    self.logger.info(
                        "From {} dropped: Auto-Submitted={} (RFC 3834)",
                        sender, auto_submitted,
                    )
                    self._remember_processed_uid(uid, dedupe, cycle_uids)
                    if mark_seen:
                        client.store(imap_id, "+FLAGS", "\\Seen")
                    continue

                # --- Anti-spoofing: verify Authentication-Results ---
                spf_pass, dkim_pass = self._check_authentication_results(parsed)
                if self.config.verify_spf and not spf_pass:
                    self.logger.warning(
                        "From {} rejected: SPF verification failed "
                        "(no 'spf=pass' in Authentication-Results header)",
                        sender,
                    )
                    self._remember_processed_uid(uid, dedupe, cycle_uids)
                    continue
                if self.config.verify_dkim and not dkim_pass:
                    self.logger.warning(
                        "From {} rejected: DKIM verification failed "
                        "(no 'dkim=pass' in Authentication-Results header)",
                        sender,
                    )
                    self._remember_processed_uid(uid, dedupe, cycle_uids)
                    continue

                if not self.is_allowed(sender):
                    self._remember_processed_uid(uid, dedupe, cycle_uids)
                    if mark_seen:
                        client.store(imap_id, "+FLAGS", "\\Seen")
                    continue

                subject = self._decode_header_value(parsed.get("Subject", ""))
                date_value = parsed.get("Date", "")
                message_id = ensure_angle_brackets(parsed.get("Message-ID", ""))
                in_reply_to = ensure_angle_brackets(parsed.get("In-Reply-To", ""))
                references = [
                    ensure_angle_brackets(r)
                    for r in (parsed.get("References", "") or "").split()
                    if r.strip()
                ]
                # Folded RFC 2822 headers can carry embedded whitespace; collapse
                # it all before base64-decoding the Thread-Index fingerprint.
                thread_index = "".join((parsed.get("Thread-Index", "") or "").split())
                thread_topic = self._decode_header_value(parsed.get("Thread-Topic", ""))
                body = self._extract_text_body(parsed)

                if not body:
                    body = "(empty email body)"

                body = body[: self.config.max_body_chars]
                content = (
                    f"[EMAIL-CONTEXT] Email received.\n"
                    f"From: {sender}\n"
                    f"Subject: {subject}\n"
                    f"Date: {date_value}\n\n"
                    f"{body}"
                )

                # --- Attachment extraction ---
                attachment_paths: list[str] = []
                if self.config.allowed_attachment_types:
                    saved = self._extract_attachments(
                        parsed,
                        uid or "noid",
                        allowed_types=self.config.allowed_attachment_types,
                        max_size=self.config.max_attachment_size,
                        max_count=self.config.max_attachments_per_email,
                    )
                    for p in saved:
                        attachment_paths.append(str(p))
                        content += f"\n[attachment: {p.name} — saved to {p}]"

                metadata = {
                    "message_id": message_id,
                    "subject": subject,
                    "date": date_value,
                    "sender_email": sender,
                    "uid": uid,
                    "in_reply_to": in_reply_to,
                    "references": references,
                    "thread_index": thread_index,
                    "thread_topic": thread_topic,
                }
                messages.append(
                    {
                        "sender": sender,
                        "subject": subject,
                        "message_id": message_id,
                        "content": content,
                        "metadata": metadata,
                        "media": attachment_paths,
                        "in_reply_to": in_reply_to,
                        "references": references,
                        "thread_index": thread_index,
                        "thread_topic": thread_topic,
                    }
                )

                self._remember_processed_uid(uid, dedupe, cycle_uids)

                if mark_seen:
                    client.store(imap_id, "+FLAGS", "\\Seen")
        finally:
            with suppress(Exception):
                client.logout()

    def _collect_self_addresses(self) -> set[str]:
        """Return normalized email addresses owned by this channel instance."""
        candidates = (
            self.config.from_address,
            self.config.smtp_username,
            self.config.imap_username,
        )
        normalized = {
            addr
            for candidate in candidates
            if (addr := self._normalize_address(candidate))
        }
        return normalized

    @staticmethod
    def _normalize_address(value: str) -> str:
        """Normalize an address or mailbox-like identifier for comparisons."""
        raw = (value or "").strip()
        if not raw:
            return ""
        parsed = parseaddr(raw)[1].strip().lower()
        if parsed:
            return parsed
        if "@" in raw:
            return raw.lower()
        return ""

    def _is_self_address(self, sender: str) -> bool:
        """Return True when an inbound sender belongs to the bot itself."""
        normalized_sender = self._normalize_address(sender)
        return bool(normalized_sender) and normalized_sender in self._self_addresses

    def _remember_processed_uid(self, uid: str, dedupe: bool, cycle_uids: set[str]) -> None:
        """Track a fetched UID so skipped messages are not reprocessed forever."""
        if not uid:
            return
        cycle_uids.add(uid)
        if dedupe:
            self._processed_uids.add(uid)
            # mark_seen is the primary dedup; this set is a safety net
            if len(self._processed_uids) > self._MAX_PROCESSED_UIDS:
                # Evict a random half to cap memory; mark_seen is the primary dedup
                self._processed_uids = set(list(self._processed_uids)[len(self._processed_uids) // 2:])

    @classmethod
    def _is_stale_imap_error(cls, exc: Exception) -> bool:
        message = str(exc).lower()
        return any(marker in message for marker in cls._IMAP_RECONNECT_MARKERS)

    @classmethod
    def _is_missing_mailbox_error(cls, exc: Exception) -> bool:
        message = str(exc).lower()
        return any(marker in message for marker in cls._IMAP_MISSING_MAILBOX_MARKERS)

    @classmethod
    def _format_imap_date(cls, value: date) -> str:
        """Format date for IMAP search (always English month abbreviations)."""
        month = cls._IMAP_MONTHS[value.month - 1]
        return f"{value.day:02d}-{month}-{value.year}"

    @staticmethod
    def _extract_message_bytes(fetched: list[Any]) -> bytes | None:
        for item in fetched:
            if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], (bytes, bytearray)):
                return bytes(item[1])
        return None

    @staticmethod
    def _extract_uid(fetched: list[Any]) -> str:
        for item in fetched:
            if isinstance(item, tuple) and item and isinstance(item[0], (bytes, bytearray)):
                head = bytes(item[0]).decode("utf-8", errors="ignore")
                m = re.search(r"UID\s+(\d+)", head)
                if m:
                    return m.group(1)
        return ""

    @staticmethod
    def _decode_header_value(value: str) -> str:
        if not value:
            return ""
        try:
            return str(make_header(decode_header(value)))
        except Exception:
            return value

    @classmethod
    def _extract_text_body(cls, msg: Any) -> str:
        """Best-effort extraction of readable body text."""
        if msg.is_multipart():
            plain_parts: list[str] = []
            html_parts: list[str] = []
            for part in msg.walk():
                if part.get_content_disposition() == "attachment":
                    continue
                content_type = part.get_content_type()
                try:
                    payload = part.get_content()
                except Exception:
                    payload_bytes = part.get_payload(decode=True) or b""
                    charset = part.get_content_charset() or "utf-8"
                    payload = payload_bytes.decode(charset, errors="replace")
                if not isinstance(payload, str):
                    continue
                if content_type == "text/plain":
                    plain_parts.append(payload)
                elif content_type == "text/html":
                    html_parts.append(payload)
            if plain_parts:
                return "\n\n".join(plain_parts).strip()
            if html_parts:
                return cls._html_to_text("\n\n".join(html_parts)).strip()
            return ""

        try:
            payload = msg.get_content()
        except Exception:
            payload_bytes = msg.get_payload(decode=True) or b""
            charset = msg.get_content_charset() or "utf-8"
            payload = payload_bytes.decode(charset, errors="replace")
        if not isinstance(payload, str):
            return ""
        if msg.get_content_type() == "text/html":
            return cls._html_to_text(payload).strip()
        return payload.strip()

    @staticmethod
    def _check_authentication_results(parsed_msg: Any) -> tuple[bool, bool]:
        """Parse Authentication-Results headers for SPF and DKIM verdicts.

        Returns:
            A tuple of (spf_pass, dkim_pass) booleans.
        """
        spf_pass = False
        dkim_pass = False
        for ar_header in parsed_msg.get_all("Authentication-Results") or []:
            ar_lower = ar_header.lower()
            if re.search(r"\bspf\s*=\s*pass\b", ar_lower):
                spf_pass = True
            if re.search(r"\bdkim\s*=\s*pass\b", ar_lower):
                dkim_pass = True
        return spf_pass, dkim_pass

    @classmethod
    def _extract_attachments(
        cls,
        msg: Any,
        uid: str,
        *,
        allowed_types: list[str],
        max_size: int,
        max_count: int,
    ) -> list[Path]:
        """Extract and save email attachments to the media directory.

        Returns list of saved file paths.
        """
        if not msg.is_multipart():
            return []

        saved: list[Path] = []
        media_dir = get_media_dir("email")

        for part in msg.walk():
            if len(saved) >= max_count:
                break
            if part.get_content_disposition() != "attachment":
                continue

            content_type = part.get_content_type()
            if not any(fnmatch(content_type, pat) for pat in allowed_types):
                logger.debug("Attachment skipped (type {}): not in allowed list", content_type)
                continue

            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            if len(payload) > max_size:
                logger.warning(
                    "Attachment skipped: size {} exceeds limit {}",
                    len(payload),
                    max_size,
                )
                continue

            raw_name = part.get_filename() or "attachment"
            sanitized = safe_filename(raw_name) or "attachment"
            dest = media_dir / f"{uid}_{sanitized}"

            try:
                dest.write_bytes(payload)
                saved.append(dest)
                logger.info("Attachment saved: {}", dest)
            except Exception as exc:
                logger.warning("Failed to save attachment {}: {}", dest, exc)

        return saved

    @staticmethod
    def _html_to_text(raw_html: str) -> str:
        text = re.sub(r"<\s*br\s*/?>", "\n", raw_html, flags=re.IGNORECASE)
        text = re.sub(r"<\s*/\s*p\s*>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", "", text)
        return html.unescape(text)

    def _resolve_thread(self, item: dict[str, Any]) -> str:
        """Resolve an inbound mail to its thread digest and record it.

        Order: References/In-Reply-To chain → Thread-Index conversation
        fingerprint gated by normalized subject (Exchange rewrites References
        on internal hops) → new thread rooted at this mail's Message-ID.
        """
        message_id = item.get("message_id", "")
        references: list[str] = item.get("references") or []
        in_reply_to = item.get("in_reply_to", "")
        subject = item.get("subject", "")
        conv_id = decode_thread_index_conv_id(item.get("thread_index", ""))

        root = references[0] if references else in_reply_to
        digest = thread_digest(root) if root else ""
        if not digest:
            via_conv = self._store.lookup_conv(conv_id, normalize_subject(subject))
            if via_conv:
                digest = via_conv
                root = self._store.get(digest)["root"]
        if not digest:
            # Header-less mail must not share one thread identity: fall back to a
            # per-message synthetic root keyed on the IMAP UID when Message-ID is empty.
            root = message_id or f"<uid-{(item.get('metadata') or {}).get('uid') or 'noid'}@durin.invalid>"
            digest = thread_digest(root)

        self._store.upsert_inbound(
            digest,
            root=root,
            address=item.get("sender", ""),
            subject=subject,
            references=references,
            message_id=message_id,
            thread_index_conv_id=conv_id,
            thread_topic=item.get("thread_topic", ""),
        )
        return digest

    def _reply_subject(self, base_subject: str) -> str:
        subject = (base_subject or "").strip() or "durin reply"
        prefix = self.config.subject_prefix or "Re: "
        if subject.lower().startswith("re:"):
            return subject
        return f"{prefix}{subject}"
