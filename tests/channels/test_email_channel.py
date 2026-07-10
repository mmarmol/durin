import imaplib
import re
from datetime import date
from email.message import EmailMessage
from pathlib import Path

import pytest

from durin.bus.events import OutboundMessage
from durin.bus.queue import MessageBus
from durin.channels.email import EmailChannel, EmailConfig


def _make_config(**overrides) -> EmailConfig:
    defaults = dict(
        enabled=True,
        consent_granted=True,
        imap_host="imap.example.com",
        imap_port=993,
        imap_username="bot@example.com",
        imap_password="secret",
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_username="bot@example.com",
        smtp_password="secret",
        mark_seen=True,
        allow_from=["*"],
        # Disable auth verification by default so existing tests are unaffected
        verify_dkim=False,
        verify_spf=False,
    )
    defaults.update(overrides)
    return EmailConfig(**defaults)


def _make_raw_email(
    from_addr: str = "alice@example.com",
    subject: str = "Hello",
    body: str = "This is the body.",
    auth_results: str | None = None,
) -> bytes:
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = "bot@example.com"
    msg["Subject"] = subject
    msg["Message-ID"] = "<m1@example.com>"
    if auth_results:
        msg["Authentication-Results"] = auth_results
    msg.set_content(body)
    return msg.as_bytes()


def test_fetch_new_messages_parses_unseen_and_marks_seen(monkeypatch) -> None:
    raw = _make_raw_email(subject="Invoice", body="Please pay")

    class FakeIMAP:
        def __init__(self) -> None:
            self.store_calls: list[tuple[bytes, str, str]] = []

        def login(self, _user: str, _pw: str):
            return "OK", [b"logged in"]

        def select(self, _mailbox: str):
            return "OK", [b"1"]

        def search(self, *_args):
            return "OK", [b"1"]

        def fetch(self, _imap_id: bytes, _parts: str):
            return "OK", [(b"1 (UID 123 BODY[] {200})", raw), b")"]

        def store(self, imap_id: bytes, op: str, flags: str):
            self.store_calls.append((imap_id, op, flags))
            return "OK", [b""]

        def logout(self):
            return "BYE", [b""]

    fake = FakeIMAP()
    monkeypatch.setattr("durin.channels.email.imaplib.IMAP4_SSL", lambda _h, _p, **_kw: fake)

    channel = EmailChannel(_make_config(), MessageBus())
    items = channel._fetch_new_messages()

    assert len(items) == 1
    assert items[0]["sender"] == "alice@example.com"
    assert items[0]["subject"] == "Invoice"
    assert "Please pay" in items[0]["content"]
    assert fake.store_calls == [(b"1", "+FLAGS", "\\Seen")]

    # Same UID should be deduped in-process.
    items_again = channel._fetch_new_messages()
    assert items_again == []


def test_fetch_new_messages_skips_self_sent_email_and_marks_seen(monkeypatch) -> None:
    raw = _make_raw_email(from_addr="Durin <bot@example.com>", subject="Loop test")

    class FakeIMAP:
        def __init__(self) -> None:
            self.store_calls: list[tuple[bytes, str, str]] = []

        def login(self, _user: str, _pw: str):
            return "OK", [b"logged in"]

        def select(self, _mailbox: str):
            return "OK", [b"1"]

        def search(self, *_args):
            return "OK", [b"1"]

        def fetch(self, _imap_id: bytes, _parts: str):
            return "OK", [(b"1 (UID 123 BODY[] {200})", raw), b")"]

        def store(self, imap_id: bytes, op: str, flags: str):
            self.store_calls.append((imap_id, op, flags))
            return "OK", [b""]

        def logout(self):
            return "BYE", [b""]

    fake = FakeIMAP()
    monkeypatch.setattr("durin.channels.email.imaplib.IMAP4_SSL", lambda _h, _p, **_kw: fake)

    channel = EmailChannel(_make_config(from_address="bot@example.com"), MessageBus())
    items = channel._fetch_new_messages()

    assert items == []
    assert fake.store_calls == [(b"1", "+FLAGS", "\\Seen")]

    # Same UID should still be deduped after being ignored.
    items_again = channel._fetch_new_messages()
    assert items_again == []


@pytest.mark.parametrize(
    "config_override,from_header",
    [
        # Only smtp_username matches — simulates an SMTP relay where
        # outbound From gets rewritten to the SMTP login identity.
        (
            {"from_address": "", "smtp_username": "bot@example.com", "imap_username": "other@imap.com"},
            "bot@example.com",
        ),
        # Only imap_username matches — simulates mailbox-based identity
        # with no explicit from_address set.
        (
            {"from_address": "", "smtp_username": "other@smtp.com", "imap_username": "bot@example.com"},
            "bot@example.com",
        ),
        # Case-insensitive: inbound From arrives upper-cased.
        (
            {"from_address": "bot@example.com", "smtp_username": "other@smtp.com", "imap_username": "other@imap.com"},
            "BOT@EXAMPLE.COM",
        ),
    ],
    ids=["smtp_username_only", "imap_username_only", "case_insensitive"],
)
def test_fetch_new_messages_skips_self_sent_across_identity_sources(
    monkeypatch, config_override, from_header
) -> None:
    """Self-address detection must fire when any of from_address / smtp_username /
    imap_username matches, and must be case-insensitive."""
    raw = _make_raw_email(from_addr=from_header, subject="Loop test")

    class FakeIMAP:
        def __init__(self) -> None:
            self.store_calls: list[tuple[bytes, str, str]] = []

        def login(self, _user: str, _pw: str):
            return "OK", [b"logged in"]

        def select(self, _mailbox: str):
            return "OK", [b"1"]

        def search(self, *_args):
            return "OK", [b"1"]

        def fetch(self, _imap_id: bytes, _parts: str):
            return "OK", [(b"1 (UID 123 BODY[] {200})", raw), b")"]

        def store(self, imap_id: bytes, op: str, flags: str):
            self.store_calls.append((imap_id, op, flags))
            return "OK", [b""]

        def logout(self):
            return "BYE", [b""]

    fake = FakeIMAP()
    monkeypatch.setattr("durin.channels.email.imaplib.IMAP4_SSL", lambda _h, _p, **_kw: fake)

    channel = EmailChannel(_make_config(**config_override), MessageBus())
    items = channel._fetch_new_messages()

    assert items == []
    assert fake.store_calls == [(b"1", "+FLAGS", "\\Seen")]


def test_fetch_new_messages_retries_once_when_imap_connection_goes_stale(monkeypatch) -> None:
    raw = _make_raw_email(subject="Invoice", body="Please pay")
    fail_once = {"pending": True}

    class FlakyIMAP:
        def __init__(self) -> None:
            self.store_calls: list[tuple[bytes, str, str]] = []
            self.search_calls = 0

        def login(self, _user: str, _pw: str):
            return "OK", [b"logged in"]

        def select(self, _mailbox: str):
            return "OK", [b"1"]

        def search(self, *_args):
            self.search_calls += 1
            if fail_once["pending"]:
                fail_once["pending"] = False
                raise imaplib.IMAP4.abort("socket error")
            return "OK", [b"1"]

        def fetch(self, _imap_id: bytes, _parts: str):
            return "OK", [(b"1 (UID 123 BODY[] {200})", raw), b")"]

        def store(self, imap_id: bytes, op: str, flags: str):
            self.store_calls.append((imap_id, op, flags))
            return "OK", [b""]

        def logout(self):
            return "BYE", [b""]

    fake_instances: list[FlakyIMAP] = []

    def _factory(_host: str, _port: int, **_kw):
        instance = FlakyIMAP()
        fake_instances.append(instance)
        return instance

    monkeypatch.setattr("durin.channels.email.imaplib.IMAP4_SSL", _factory)

    channel = EmailChannel(_make_config(), MessageBus())
    items = channel._fetch_new_messages()

    assert len(items) == 1
    assert len(fake_instances) == 2
    assert fake_instances[0].search_calls == 1
    assert fake_instances[1].search_calls == 1


def test_fetch_new_messages_keeps_messages_collected_before_stale_retry(monkeypatch) -> None:
    raw_first = _make_raw_email(subject="First", body="First body")
    raw_second = _make_raw_email(subject="Second", body="Second body")
    mailbox_state = {
        b"1": {"uid": b"123", "raw": raw_first, "seen": False},
        b"2": {"uid": b"124", "raw": raw_second, "seen": False},
    }
    fail_once = {"pending": True}

    class FlakyIMAP:
        def login(self, _user: str, _pw: str):
            return "OK", [b"logged in"]

        def select(self, _mailbox: str):
            return "OK", [b"2"]

        def search(self, *_args):
            unseen_ids = [imap_id for imap_id, item in mailbox_state.items() if not item["seen"]]
            return "OK", [b" ".join(unseen_ids)]

        def fetch(self, imap_id: bytes, _parts: str):
            if imap_id == b"2" and fail_once["pending"]:
                fail_once["pending"] = False
                raise imaplib.IMAP4.abort("socket error")
            item = mailbox_state[imap_id]
            header = b"%s (UID %s BODY[] {200})" % (imap_id, item["uid"])
            return "OK", [(header, item["raw"]), b")"]

        def store(self, imap_id: bytes, _op: str, _flags: str):
            mailbox_state[imap_id]["seen"] = True
            return "OK", [b""]

        def logout(self):
            return "BYE", [b""]

    monkeypatch.setattr("durin.channels.email.imaplib.IMAP4_SSL", lambda _h, _p, **_kw: FlakyIMAP())

    channel = EmailChannel(_make_config(), MessageBus())
    items = channel._fetch_new_messages()

    assert [item["subject"] for item in items] == ["First", "Second"]


def test_fetch_new_messages_skips_missing_mailbox(monkeypatch) -> None:
    class MissingMailboxIMAP:
        def login(self, _user: str, _pw: str):
            return "OK", [b"logged in"]

        def select(self, _mailbox: str):
            raise imaplib.IMAP4.error("Mailbox doesn't exist")

        def logout(self):
            return "BYE", [b""]

    monkeypatch.setattr(
        "durin.channels.email.imaplib.IMAP4_SSL",
        lambda _h, _p, **_kw: MissingMailboxIMAP(),
    )

    channel = EmailChannel(_make_config(), MessageBus())

    assert channel._fetch_new_messages() == []


def test_extract_text_body_falls_back_to_html() -> None:
    msg = EmailMessage()
    msg["From"] = "alice@example.com"
    msg["To"] = "bot@example.com"
    msg["Subject"] = "HTML only"
    msg.add_alternative("<p>Hello<br>world</p>", subtype="html")

    text = EmailChannel._extract_text_body(msg)
    assert "Hello" in text
    assert "world" in text


@pytest.mark.asyncio
async def test_start_returns_immediately_without_consent(monkeypatch) -> None:
    cfg = _make_config()
    cfg.consent_granted = False
    channel = EmailChannel(cfg, MessageBus())

    called = {"fetch": False}

    def _fake_fetch():
        called["fetch"] = True
        return []

    monkeypatch.setattr(channel, "_fetch_new_messages", _fake_fetch)
    await channel.start()
    assert channel.is_running is False
    assert called["fetch"] is False


@pytest.mark.asyncio
async def test_send_uses_smtp_and_reply_subject(monkeypatch) -> None:
    from durin.channels.email_threads import thread_digest

    class FakeSMTP:
        def __init__(self, _host: str, _port: int, timeout: int = 30) -> None:
            self.timeout = timeout
            self.started_tls = False
            self.logged_in = False
            self.sent_messages: list[EmailMessage] = []

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def starttls(self, context=None):
            self.started_tls = True

        def login(self, _user: str, _pw: str):
            self.logged_in = True

        def send_message(self, msg: EmailMessage):
            self.sent_messages.append(msg)

    fake_instances: list[FakeSMTP] = []

    def _smtp_factory(host: str, port: int, timeout: int = 30):
        instance = FakeSMTP(host, port, timeout=timeout)
        fake_instances.append(instance)
        return instance

    monkeypatch.setattr("durin.channels.email.smtplib.SMTP", _smtp_factory)

    channel = EmailChannel(_make_config(), MessageBus())
    channel._store.upsert_inbound(
        thread_digest("<m1@example.com>"),
        root="<m1@example.com>",
        address="alice@example.com",
        subject="Invoice #42",
        references=[],
        message_id="<m1@example.com>",
    )

    await channel.send(
        OutboundMessage(
            channel="email",
            chat_id="alice@example.com",
            content="Acknowledged.",
        )
    )

    assert len(fake_instances) == 1
    smtp = fake_instances[0]
    assert smtp.started_tls is True
    assert smtp.logged_in is True
    assert len(smtp.sent_messages) == 1
    sent = smtp.sent_messages[0]
    assert sent["Subject"] == "Re: Invoice #42"
    assert sent["To"] == "alice@example.com"
    assert sent["In-Reply-To"] == "<m1@example.com>"
    refs = sent["References"].split()
    assert refs == ["<m1@example.com>"]
    assert sent["Message-ID"].startswith("<") and sent["Message-ID"].endswith(">")


@pytest.mark.asyncio
async def test_send_skips_reply_when_auto_reply_disabled(monkeypatch) -> None:
    """When auto_reply_enabled=False, replies should be skipped but proactive sends allowed."""
    from durin.channels.email_threads import thread_digest

    class FakeSMTP:
        def __init__(self, _host: str, _port: int, timeout: int = 30) -> None:
            self.sent_messages: list[EmailMessage] = []

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def starttls(self, context=None):
            return None

        def login(self, _user: str, _pw: str):
            return None

        def send_message(self, msg: EmailMessage):
            self.sent_messages.append(msg)

    fake_instances: list[FakeSMTP] = []

    def _smtp_factory(host: str, port: int, timeout: int = 30):
        instance = FakeSMTP(host, port, timeout=timeout)
        fake_instances.append(instance)
        return instance

    monkeypatch.setattr("durin.channels.email.smtplib.SMTP", _smtp_factory)

    cfg = _make_config()
    cfg.auto_reply_enabled = False
    channel = EmailChannel(cfg, MessageBus())

    # Mark alice as someone who sent us an email (making this a "reply")
    channel._store.upsert_inbound(
        thread_digest("<prev@example.com>"),
        root="<prev@example.com>",
        address="alice@example.com",
        subject="Previous email",
        references=[],
        message_id="<prev@example.com>",
    )

    # Reply should be skipped (auto_reply_enabled=False)
    await channel.send(
        OutboundMessage(
            channel="email",
            chat_id="alice@example.com",
            content="Should not send.",
        )
    )
    assert fake_instances == []

    # Reply with force_send=True should be sent
    await channel.send(
        OutboundMessage(
            channel="email",
            chat_id="alice@example.com",
            content="Force send.",
            metadata={"force_send": True},
        )
    )
    assert len(fake_instances) == 1
    assert len(fake_instances[0].sent_messages) == 1


@pytest.mark.asyncio
async def test_send_proactive_email_when_auto_reply_disabled(monkeypatch) -> None:
    """Proactive emails (not replies) should be sent even when auto_reply_enabled=False."""
    class FakeSMTP:
        def __init__(self, _host: str, _port: int, timeout: int = 30) -> None:
            self.sent_messages: list[EmailMessage] = []

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def starttls(self, context=None):
            return None

        def login(self, _user: str, _pw: str):
            return None

        def send_message(self, msg: EmailMessage):
            self.sent_messages.append(msg)

    fake_instances: list[FakeSMTP] = []

    def _smtp_factory(host: str, port: int, timeout: int = 30):
        instance = FakeSMTP(host, port, timeout=timeout)
        fake_instances.append(instance)
        return instance

    monkeypatch.setattr("durin.channels.email.smtplib.SMTP", _smtp_factory)

    cfg = _make_config()
    cfg.auto_reply_enabled = False
    channel = EmailChannel(cfg, MessageBus())

    # bob@example.com has never sent us an email (proactive send)
    # This should be sent even with auto_reply_enabled=False
    await channel.send(
        OutboundMessage(
            channel="email",
            chat_id="bob@example.com",
            content="Hello, this is a proactive email.",
        )
    )
    assert len(fake_instances) == 1
    assert len(fake_instances[0].sent_messages) == 1
    sent = fake_instances[0].sent_messages[0]
    assert sent["To"] == "bob@example.com"


@pytest.mark.asyncio
async def test_send_skips_when_consent_not_granted(monkeypatch) -> None:
    class FakeSMTP:
        def __init__(self, _host: str, _port: int, timeout: int = 30) -> None:
            self.sent_messages: list[EmailMessage] = []

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def starttls(self, context=None):
            return None

        def login(self, _user: str, _pw: str):
            return None

        def send_message(self, msg: EmailMessage):
            self.sent_messages.append(msg)

    called = {"smtp": False}

    def _smtp_factory(host: str, port: int, timeout: int = 30):
        called["smtp"] = True
        return FakeSMTP(host, port, timeout=timeout)

    monkeypatch.setattr("durin.channels.email.smtplib.SMTP", _smtp_factory)

    cfg = _make_config()
    cfg.consent_granted = False
    channel = EmailChannel(cfg, MessageBus())
    await channel.send(
        OutboundMessage(
            channel="email",
            chat_id="alice@example.com",
            content="Should not send.",
            metadata={"force_send": True},
        )
    )
    assert called["smtp"] is False


def test_fetch_messages_between_dates_uses_imap_since_before_without_mark_seen(monkeypatch) -> None:
    raw = _make_raw_email(subject="Status", body="Yesterday update")

    class FakeIMAP:
        def __init__(self) -> None:
            self.search_args = None
            self.store_calls: list[tuple[bytes, str, str]] = []

        def login(self, _user: str, _pw: str):
            return "OK", [b"logged in"]

        def select(self, _mailbox: str):
            return "OK", [b"1"]

        def search(self, *_args):
            self.search_args = _args
            return "OK", [b"5"]

        def fetch(self, _imap_id: bytes, _parts: str):
            return "OK", [(b"5 (UID 999 BODY[] {200})", raw), b")"]

        def store(self, imap_id: bytes, op: str, flags: str):
            self.store_calls.append((imap_id, op, flags))
            return "OK", [b""]

        def logout(self):
            return "BYE", [b""]

    fake = FakeIMAP()
    monkeypatch.setattr("durin.channels.email.imaplib.IMAP4_SSL", lambda _h, _p, **_kw: fake)

    channel = EmailChannel(_make_config(), MessageBus())
    items = channel.fetch_messages_between_dates(
        start_date=date(2026, 2, 6),
        end_date=date(2026, 2, 7),
        limit=10,
    )

    assert len(items) == 1
    assert items[0]["subject"] == "Status"
    # search(None, "SINCE", "06-Feb-2026", "BEFORE", "07-Feb-2026")
    assert fake.search_args is not None
    assert fake.search_args[1:] == ("SINCE", "06-Feb-2026", "BEFORE", "07-Feb-2026")
    assert fake.store_calls == []


# ---------------------------------------------------------------------------
# Security: Anti-spoofing tests for Authentication-Results verification
# ---------------------------------------------------------------------------

def _make_fake_imap(raw: bytes):
    """Return a FakeIMAP class pre-loaded with the given raw email."""
    class FakeIMAP:
        def __init__(self) -> None:
            self.store_calls: list[tuple[bytes, str, str]] = []

        def login(self, _user: str, _pw: str):
            return "OK", [b"logged in"]

        def select(self, _mailbox: str):
            return "OK", [b"1"]

        def search(self, *_args):
            return "OK", [b"1"]

        def fetch(self, _imap_id: bytes, _parts: str):
            return "OK", [(b"1 (UID 500 BODY[] {200})", raw), b")"]

        def store(self, imap_id: bytes, op: str, flags: str):
            self.store_calls.append((imap_id, op, flags))
            return "OK", [b""]

        def logout(self):
            return "BYE", [b""]

    return FakeIMAP()


def test_spoofed_email_rejected_when_verify_enabled(monkeypatch) -> None:
    """An email without Authentication-Results should be rejected when verify_dkim=True."""
    raw = _make_raw_email(subject="Spoofed", body="Malicious payload")
    fake = _make_fake_imap(raw)
    monkeypatch.setattr("durin.channels.email.imaplib.IMAP4_SSL", lambda _h, _p, **_kw: fake)

    cfg = _make_config(verify_dkim=True, verify_spf=True)
    channel = EmailChannel(cfg, MessageBus())
    items = channel._fetch_new_messages()

    assert len(items) == 0, "Spoofed email without auth headers should be rejected"


def test_email_with_valid_auth_results_accepted(monkeypatch) -> None:
    """An email with spf=pass and dkim=pass should be accepted."""
    raw = _make_raw_email(
        subject="Legit",
        body="Hello from verified sender",
        auth_results="mx.example.com; spf=pass smtp.mailfrom=alice@example.com; dkim=pass header.d=example.com",
    )
    fake = _make_fake_imap(raw)
    monkeypatch.setattr("durin.channels.email.imaplib.IMAP4_SSL", lambda _h, _p, **_kw: fake)

    cfg = _make_config(verify_dkim=True, verify_spf=True)
    channel = EmailChannel(cfg, MessageBus())
    items = channel._fetch_new_messages()

    assert len(items) == 1
    assert items[0]["sender"] == "alice@example.com"
    assert items[0]["subject"] == "Legit"


def test_email_with_partial_auth_rejected(monkeypatch) -> None:
    """An email with only spf=pass but no dkim=pass should be rejected when verify_dkim=True."""
    raw = _make_raw_email(
        subject="Partial",
        body="Only SPF passes",
        auth_results="mx.example.com; spf=pass smtp.mailfrom=alice@example.com; dkim=fail",
    )
    fake = _make_fake_imap(raw)
    monkeypatch.setattr("durin.channels.email.imaplib.IMAP4_SSL", lambda _h, _p, **_kw: fake)

    cfg = _make_config(verify_dkim=True, verify_spf=True)
    channel = EmailChannel(cfg, MessageBus())
    items = channel._fetch_new_messages()

    assert len(items) == 0, "Email with dkim=fail should be rejected"


def test_backward_compat_verify_disabled(monkeypatch) -> None:
    """When verify_dkim=False and verify_spf=False, emails without auth headers are accepted."""
    raw = _make_raw_email(subject="NoAuth", body="No auth headers present")
    fake = _make_fake_imap(raw)
    monkeypatch.setattr("durin.channels.email.imaplib.IMAP4_SSL", lambda _h, _p, **_kw: fake)

    cfg = _make_config(verify_dkim=False, verify_spf=False)
    channel = EmailChannel(cfg, MessageBus())
    items = channel._fetch_new_messages()

    assert len(items) == 1, "With verification disabled, emails should be accepted as before"


def test_email_content_tagged_with_email_context(monkeypatch) -> None:
    """Email content should be prefixed with [EMAIL-CONTEXT] for LLM isolation."""
    raw = _make_raw_email(subject="Tagged", body="Check the tag")
    fake = _make_fake_imap(raw)
    monkeypatch.setattr("durin.channels.email.imaplib.IMAP4_SSL", lambda _h, _p, **_kw: fake)

    cfg = _make_config(verify_dkim=False, verify_spf=False)
    channel = EmailChannel(cfg, MessageBus())
    items = channel._fetch_new_messages()

    assert len(items) == 1
    assert items[0]["content"].startswith("[EMAIL-CONTEXT]"), (
        "Email content must be tagged with [EMAIL-CONTEXT]"
    )


def test_check_authentication_results_method() -> None:
    """Unit test for the _check_authentication_results static method."""
    from email import policy
    from email.parser import BytesParser

    # No Authentication-Results header
    msg_no_auth = EmailMessage()
    msg_no_auth["From"] = "alice@example.com"
    msg_no_auth.set_content("test")
    parsed = BytesParser(policy=policy.default).parsebytes(msg_no_auth.as_bytes())
    spf, dkim = EmailChannel._check_authentication_results(parsed)
    assert spf is False
    assert dkim is False

    # Both pass
    msg_both = EmailMessage()
    msg_both["From"] = "alice@example.com"
    msg_both["Authentication-Results"] = (
        "mx.google.com; spf=pass smtp.mailfrom=example.com; dkim=pass header.d=example.com"
    )
    msg_both.set_content("test")
    parsed = BytesParser(policy=policy.default).parsebytes(msg_both.as_bytes())
    spf, dkim = EmailChannel._check_authentication_results(parsed)
    assert spf is True
    assert dkim is True

    # SPF pass, DKIM fail
    msg_spf_only = EmailMessage()
    msg_spf_only["From"] = "alice@example.com"
    msg_spf_only["Authentication-Results"] = (
        "mx.google.com; spf=pass smtp.mailfrom=example.com; dkim=fail"
    )
    msg_spf_only.set_content("test")
    parsed = BytesParser(policy=policy.default).parsebytes(msg_spf_only.as_bytes())
    spf, dkim = EmailChannel._check_authentication_results(parsed)
    assert spf is True
    assert dkim is False

    # DKIM pass, SPF fail
    msg_dkim_only = EmailMessage()
    msg_dkim_only["From"] = "alice@example.com"
    msg_dkim_only["Authentication-Results"] = (
        "mx.google.com; spf=fail smtp.mailfrom=example.com; dkim=pass header.d=example.com"
    )
    msg_dkim_only.set_content("test")
    parsed = BytesParser(policy=policy.default).parsebytes(msg_dkim_only.as_bytes())
    spf, dkim = EmailChannel._check_authentication_results(parsed)
    assert spf is False
    assert dkim is True


# ---------------------------------------------------------------------------
# Attachment extraction tests
# ---------------------------------------------------------------------------


def _make_raw_email_with_attachment(
    from_addr: str = "alice@example.com",
    subject: str = "With attachment",
    body: str = "See attached.",
    attachment_name: str = "doc.pdf",
    attachment_content: bytes = b"%PDF-1.4 fake pdf content",
    attachment_mime: str = "application/pdf",
    auth_results: str | None = None,
) -> bytes:
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = "bot@example.com"
    msg["Subject"] = subject
    msg["Message-ID"] = "<m1@example.com>"
    if auth_results:
        msg["Authentication-Results"] = auth_results
    msg.set_content(body)
    maintype, subtype = attachment_mime.split("/", 1)
    msg.add_attachment(
        attachment_content,
        maintype=maintype,
        subtype=subtype,
        filename=attachment_name,
    )
    return msg.as_bytes()


def test_fetch_new_messages_ignores_unauthorized_sender_before_attachments(monkeypatch) -> None:
    raw = _make_raw_email_with_attachment(from_addr="blocked@example.com")
    fake = _make_fake_imap(raw)
    monkeypatch.setattr("durin.channels.email.imaplib.IMAP4_SSL", lambda _h, _p, **_kw: fake)

    called = {"attachments": False}

    def _extract_attachments(*_args, **_kwargs):
        called["attachments"] = True
        return []

    monkeypatch.setattr(EmailChannel, "_extract_attachments", _extract_attachments)

    cfg = _make_config(
        allow_from=["allowed@example.com"],
        allowed_attachment_types=["application/pdf"],
        verify_dkim=False,
        verify_spf=False,
    )
    channel = EmailChannel(cfg, MessageBus())

    assert channel._fetch_new_messages() == []
    assert called["attachments"] is False
    assert fake.store_calls == [(b"1", "+FLAGS", "\\Seen")]


def test_extract_attachments_saves_pdf(tmp_path, monkeypatch) -> None:
    """PDF attachment is saved to media dir and path returned in media list."""
    monkeypatch.setattr("durin.channels.email.get_media_dir", lambda ch: tmp_path)

    raw = _make_raw_email_with_attachment()
    fake = _make_fake_imap(raw)
    monkeypatch.setattr("durin.channels.email.imaplib.IMAP4_SSL", lambda _h, _p, **_kw: fake)

    cfg = _make_config(allowed_attachment_types=["application/pdf"], verify_dkim=False, verify_spf=False)
    channel = EmailChannel(cfg, MessageBus())
    items = channel._fetch_new_messages()

    assert len(items) == 1
    assert len(items[0]["media"]) == 1
    saved_path = Path(items[0]["media"][0])
    assert saved_path.exists()
    assert saved_path.read_bytes() == b"%PDF-1.4 fake pdf content"
    assert "500_doc.pdf" in saved_path.name
    assert "[attachment:" in items[0]["content"]


def test_extract_attachments_disabled_by_default(monkeypatch) -> None:
    """With no allowed_attachment_types (default), no attachments are extracted."""
    raw = _make_raw_email_with_attachment()
    fake = _make_fake_imap(raw)
    monkeypatch.setattr("durin.channels.email.imaplib.IMAP4_SSL", lambda _h, _p, **_kw: fake)

    cfg = _make_config(verify_dkim=False, verify_spf=False)
    assert cfg.allowed_attachment_types == []
    channel = EmailChannel(cfg, MessageBus())
    items = channel._fetch_new_messages()

    assert len(items) == 1
    assert items[0]["media"] == []
    assert "[attachment:" not in items[0]["content"]


def test_extract_attachments_mime_type_filter(tmp_path, monkeypatch) -> None:
    """Non-allowed MIME types are skipped."""
    monkeypatch.setattr("durin.channels.email.get_media_dir", lambda ch: tmp_path)

    raw = _make_raw_email_with_attachment(
        attachment_name="image.png",
        attachment_content=b"\x89PNG fake",
        attachment_mime="image/png",
    )
    fake = _make_fake_imap(raw)
    monkeypatch.setattr("durin.channels.email.imaplib.IMAP4_SSL", lambda _h, _p, **_kw: fake)

    cfg = _make_config(
        allowed_attachment_types=["application/pdf"],
        verify_dkim=False,
        verify_spf=False,
    )
    channel = EmailChannel(cfg, MessageBus())
    items = channel._fetch_new_messages()

    assert len(items) == 1
    assert items[0]["media"] == []


def test_extract_attachments_empty_allowed_types_rejects_all(tmp_path, monkeypatch) -> None:
    """Empty allowed_attachment_types means no types are accepted."""
    monkeypatch.setattr("durin.channels.email.get_media_dir", lambda ch: tmp_path)

    raw = _make_raw_email_with_attachment(
        attachment_name="image.png",
        attachment_content=b"\x89PNG fake",
        attachment_mime="image/png",
    )
    fake = _make_fake_imap(raw)
    monkeypatch.setattr("durin.channels.email.imaplib.IMAP4_SSL", lambda _h, _p, **_kw: fake)

    cfg = _make_config(
        allowed_attachment_types=[],
        verify_dkim=False,
        verify_spf=False,
    )
    channel = EmailChannel(cfg, MessageBus())
    items = channel._fetch_new_messages()

    assert len(items) == 1
    assert items[0]["media"] == []


def test_extract_attachments_wildcard_pattern(tmp_path, monkeypatch) -> None:
    """Glob patterns like 'image/*' match attachment MIME types."""
    monkeypatch.setattr("durin.channels.email.get_media_dir", lambda ch: tmp_path)

    raw = _make_raw_email_with_attachment(
        attachment_name="photo.jpg",
        attachment_content=b"\xff\xd8\xff fake jpeg",
        attachment_mime="image/jpeg",
    )
    fake = _make_fake_imap(raw)
    monkeypatch.setattr("durin.channels.email.imaplib.IMAP4_SSL", lambda _h, _p, **_kw: fake)

    cfg = _make_config(
        allowed_attachment_types=["image/*"],
        verify_dkim=False,
        verify_spf=False,
    )
    channel = EmailChannel(cfg, MessageBus())
    items = channel._fetch_new_messages()

    assert len(items) == 1
    assert len(items[0]["media"]) == 1


def test_extract_attachments_size_limit(tmp_path, monkeypatch) -> None:
    """Attachments exceeding max_attachment_size are skipped."""
    monkeypatch.setattr("durin.channels.email.get_media_dir", lambda ch: tmp_path)

    raw = _make_raw_email_with_attachment(
        attachment_content=b"x" * 1000,
    )
    fake = _make_fake_imap(raw)
    monkeypatch.setattr("durin.channels.email.imaplib.IMAP4_SSL", lambda _h, _p, **_kw: fake)

    cfg = _make_config(
        allowed_attachment_types=["*"],
        max_attachment_size=500,
        verify_dkim=False,
        verify_spf=False,
    )
    channel = EmailChannel(cfg, MessageBus())
    items = channel._fetch_new_messages()

    assert len(items) == 1
    assert items[0]["media"] == []


def test_extract_attachments_max_count(tmp_path, monkeypatch) -> None:
    """Only max_attachments_per_email are saved."""
    monkeypatch.setattr("durin.channels.email.get_media_dir", lambda ch: tmp_path)

    # Build email with 3 attachments
    msg = EmailMessage()
    msg["From"] = "alice@example.com"
    msg["To"] = "bot@example.com"
    msg["Subject"] = "Many attachments"
    msg["Message-ID"] = "<m1@example.com>"
    msg.set_content("See attached.")
    for i in range(3):
        msg.add_attachment(
            f"content {i}".encode(),
            maintype="application",
            subtype="pdf",
            filename=f"doc{i}.pdf",
        )
    raw = msg.as_bytes()

    fake = _make_fake_imap(raw)
    monkeypatch.setattr("durin.channels.email.imaplib.IMAP4_SSL", lambda _h, _p, **_kw: fake)

    cfg = _make_config(
        allowed_attachment_types=["*"],
        max_attachments_per_email=2,
        verify_dkim=False,
        verify_spf=False,
    )
    channel = EmailChannel(cfg, MessageBus())
    items = channel._fetch_new_messages()

    assert len(items) == 1
    assert len(items[0]["media"]) == 2


def test_extract_attachments_sanitizes_filename(tmp_path, monkeypatch) -> None:
    """Path traversal in filenames is neutralized."""
    monkeypatch.setattr("durin.channels.email.get_media_dir", lambda ch: tmp_path)

    raw = _make_raw_email_with_attachment(
        attachment_name="../../../etc/passwd",
    )
    fake = _make_fake_imap(raw)
    monkeypatch.setattr("durin.channels.email.imaplib.IMAP4_SSL", lambda _h, _p, **_kw: fake)

    cfg = _make_config(allowed_attachment_types=["*"], verify_dkim=False, verify_spf=False)
    channel = EmailChannel(cfg, MessageBus())
    items = channel._fetch_new_messages()

    assert len(items) == 1
    assert len(items[0]["media"]) == 1
    saved_path = Path(items[0]["media"][0])
    # File must be inside the media dir, not escaped via path traversal
    assert saved_path.parent == tmp_path


# ---------------------------------------------------------------------------
# Threading: RFC 3834 guard, thread resolution, per-thread session keys
# ---------------------------------------------------------------------------


def _make_raw_email_threaded(
    from_addr: str = "alice@example.com",
    subject: str = "Hello",
    body: str = "This is the body.",
    message_id: str = "<m1@example.com>",
    in_reply_to: str | None = None,
    references: str | None = None,
    thread_index: str | None = None,
    auto_submitted: str | None = None,
) -> bytes:
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = "bot@example.com"
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    if thread_index:
        msg["Thread-Index"] = thread_index
    if auto_submitted:
        msg["Auto-Submitted"] = auto_submitted
    msg.set_content(body)
    return msg.as_bytes()


class _FakeIMAPSingle:
    """Serves one raw message for every fetch cycle."""

    def __init__(self, raw: bytes, uid: str = "123") -> None:
        self.raw = raw
        self.uid = uid
        self.store_calls: list[tuple[bytes, str, str]] = []

    def login(self, _u, _p):
        return "OK", [b"ok"]

    def select(self, _m):
        return "OK", [b"1"]

    def search(self, *_a):
        return "OK", [b"1"]

    def fetch(self, _i, _p):
        return "OK", [(f"1 (UID {self.uid} BODY[] {{200}})".encode(), self.raw), b")"]

    def store(self, imap_id, op, flags):
        self.store_calls.append((imap_id, op, flags))
        return "OK", [b""]

    def logout(self):
        return "BYE", [b""]


def _make_channel(tmp_path, monkeypatch, raw: bytes, uid: str = "123", **cfg):
    fake = _FakeIMAPSingle(raw, uid=uid)
    monkeypatch.setattr("durin.channels.email.imaplib.IMAP4_SSL", lambda _h, _p, **_kw: fake)
    monkeypatch.setenv("DURIN_HOME", str(tmp_path))
    channel = EmailChannel(_make_config(**cfg), MessageBus())
    return channel, fake


def test_auto_submitted_mail_is_dropped(tmp_path, monkeypatch) -> None:
    raw = _make_raw_email_threaded(auto_submitted="auto-replied")
    channel, fake = _make_channel(tmp_path, monkeypatch, raw)
    assert channel._fetch_new_messages() == []
    # Still marked seen so it is not re-fetched forever.
    assert fake.store_calls == [(b"1", "+FLAGS", "\\Seen")]


def test_auto_submitted_no_is_processed(tmp_path, monkeypatch) -> None:
    raw = _make_raw_email_threaded(auto_submitted="no")
    channel, _ = _make_channel(tmp_path, monkeypatch, raw)
    assert len(channel._fetch_new_messages()) == 1


def test_inbound_captures_threading_headers(tmp_path, monkeypatch) -> None:
    raw = _make_raw_email_threaded(
        message_id="<m3@example.com>",
        in_reply_to="<m2@example.com>",
        references="<m1@example.com> <m2@example.com>",
    )
    channel, _ = _make_channel(tmp_path, monkeypatch, raw)
    items = channel._fetch_new_messages()
    assert items[0]["references"] == ["<m1@example.com>", "<m2@example.com>"]
    assert items[0]["in_reply_to"] == "<m2@example.com>"


def test_resolve_thread_reply_joins_existing_thread(tmp_path, monkeypatch) -> None:
    from durin.channels.email_threads import thread_digest

    first = _make_raw_email_threaded(message_id="<m1@example.com>")
    channel, _ = _make_channel(tmp_path, monkeypatch, first)
    item1 = channel._fetch_new_messages()[0]
    d1 = channel._resolve_thread(item1)
    assert d1 == thread_digest("<m1@example.com>")

    reply = _make_raw_email_threaded(
        message_id="<m2@example.com>",
        references="<m1@example.com>",
        subject="Re: Hello",
    )
    channel2, _ = _make_channel(tmp_path, monkeypatch, reply, uid="124")
    item2 = channel2._fetch_new_messages()[0]
    assert channel2._resolve_thread(item2) == d1
    entry = channel2._store.get(d1)
    assert entry["last_message_id"] == "<m2@example.com>"
    assert entry["references"] == ["<m1@example.com>", "<m2@example.com>"]


def test_resolve_thread_thread_index_fallback(tmp_path, monkeypatch) -> None:
    import base64

    conv = base64.b64encode(bytes(range(22))).decode()
    first = _make_raw_email_threaded(
        message_id="<m1@example.com>", subject="Budget", thread_index=conv
    )
    channel, _ = _make_channel(tmp_path, monkeypatch, first)
    d1 = channel._resolve_thread(channel._fetch_new_messages()[0])

    # Exchange dropped References but kept the conversation prefix.
    reply = _make_raw_email_threaded(
        message_id="<m9@example.com>", subject="RE: Budget",
        thread_index=base64.b64encode(bytes(range(22)) + b"\x01\x02\x03\x04\x05").decode(),
    )
    channel2, _ = _make_channel(tmp_path, monkeypatch, reply, uid="124")
    assert channel2._resolve_thread(channel2._fetch_new_messages()[0]) == d1

    # Same fingerprint, different subject → NOT merged.
    other = _make_raw_email_threaded(
        message_id="<mX@example.com>", subject="Totally different",
        thread_index=conv,
    )
    channel3, _ = _make_channel(tmp_path, monkeypatch, other, uid="125")
    assert channel3._resolve_thread(channel3._fetch_new_messages()[0]) != d1


def test_resolve_thread_header_less_mail_does_not_collapse_threads(tmp_path, monkeypatch) -> None:
    """Two mails with no Message-ID/References/In-Reply-To/Thread-Index must not
    collapse into the same thread — each gets its own digest from the UID."""
    first = _make_raw_email_threaded(message_id="")
    channel, _ = _make_channel(tmp_path, monkeypatch, first, uid="123")
    item1 = channel._fetch_new_messages()[0]
    d1 = channel._resolve_thread(item1)

    second = _make_raw_email_threaded(message_id="")
    channel2, _ = _make_channel(tmp_path, monkeypatch, second, uid="124")
    item2 = channel2._fetch_new_messages()[0]
    d2 = channel2._resolve_thread(item2)

    assert d1 != d2


@pytest.mark.asyncio
async def test_session_key_override_per_mode(tmp_path, monkeypatch) -> None:
    from durin.channels.email_threads import thread_digest

    raw = _make_raw_email_threaded(message_id="<m1@example.com>")
    published: list = []

    async def _capture(msg):
        published.append(msg)

    for mode, expected in (
        ("thread", f"email:alice@example.com:{thread_digest('<m1@example.com>')}"),
        ("sender", "email:alice@example.com"),
    ):
        published.clear()
        channel, _ = _make_channel(tmp_path, monkeypatch, raw, threading_mode=mode)
        monkeypatch.setattr(channel.bus, "publish_inbound", _capture)
        channel._store.load()
        items = channel._fetch_new_messages()
        digest = channel._resolve_thread(items[0])
        await channel._handle_message(
            sender_id=items[0]["sender"],
            chat_id=items[0]["sender"],
            content=items[0]["content"],
            metadata=items[0]["metadata"],
            session_key=(
                f"email:{items[0]['sender']}:{digest}" if mode == "thread" else None
            ),
        )
        assert published[0].session_key == expected


@pytest.mark.asyncio
async def test_start_polling_loop_publishes_thread_scoped_session(tmp_path, monkeypatch) -> None:
    """Exercises the real start() polling loop end-to-end — the _resolve_thread call,
    the threading_mode session-key ternary, and the _handle_message call — rather than
    re-deriving the ternary manually against a hand-called _fetch_new_messages/_resolve_thread."""
    import durin.channels.email as email_module
    from durin.channels.email_threads import thread_digest

    class _FakeAsyncio:
        """Delegates to the real asyncio module except sleep, which is a no-op so the
        polling loop doesn't actually wait poll_interval_seconds between cycles."""

        def __init__(self, real):
            self._real = real

        async def sleep(self, _seconds):
            return None

        def __getattr__(self, name):
            return getattr(self._real, name)

    monkeypatch.setattr(email_module, "asyncio", _FakeAsyncio(email_module.asyncio))

    for mode, expected in (
        ("thread", f"email:alice@example.com:{thread_digest('<m1@example.com>')}"),
        ("sender", "email:alice@example.com"),
    ):
        raw = _make_raw_email_threaded(message_id="<m1@example.com>")
        channel, _ = _make_channel(tmp_path, monkeypatch, raw, threading_mode=mode)

        published: list = []

        async def _capture(msg):
            published.append(msg)
            channel._running = False

        monkeypatch.setattr(channel.bus, "publish_inbound", _capture)

        await channel.start()

        assert len(published) == 1
        assert published[0].session_key == expected
        assert channel._store.get(thread_digest("<m1@example.com>")) is not None
        # The start() loop must embed the thread digest into inbound metadata
        # in BOTH modes — ordinary replies flow through the agent loop's
        # generic inbound-metadata passthrough (_assemble_outbound), not the
        # system/subagent-followup builder, so without this the digest never
        # reaches send() for a normal reply.
        assert published[0].metadata.get("email") == {
            "thread": thread_digest("<m1@example.com>")
        }


# ---------------------------------------------------------------------------
# Outbound: stitching from the store + agent-loop routing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_stitches_full_reference_chain(tmp_path, monkeypatch) -> None:
    raw = _make_raw_email_threaded(
        message_id="<m2@example.com>", references="<m1@example.com>",
        subject="Re: Invoice",
    )
    channel, _ = _make_channel(tmp_path, monkeypatch, raw)
    channel._store.load()
    item = channel._fetch_new_messages()[0]
    digest = channel._resolve_thread(item)

    sent: list = []
    monkeypatch.setattr(channel, "_smtp_send", lambda m: sent.append(m))
    await channel.send(OutboundMessage(
        channel="email", chat_id="alice@example.com", content="On it.",
        metadata={"email": {"thread": digest}},
    ))

    msg = sent[0]
    assert msg["In-Reply-To"] == "<m2@example.com>"
    refs = msg["References"].split()
    assert refs == ["<m1@example.com>", "<m2@example.com>"]
    assert msg["Subject"] == "Re: Invoice"
    own_id = msg["Message-ID"]
    assert own_id.startswith("<") and own_id.endswith(">")
    # durin's own message becomes the thread's new last link.
    entry = channel._store.get(digest)
    assert entry["last_message_id"] == own_id
    assert entry["references"][-1] == own_id


@pytest.mark.asyncio
async def test_send_two_parallel_threads_do_not_cross(tmp_path, monkeypatch) -> None:
    channel = None
    digests = []
    for mid, subj, uid in (("<a1@x>", "Thread A", "1"), ("<b1@x>", "Thread B", "2")):
        raw = _make_raw_email_threaded(message_id=mid, subject=subj)
        channel, _ = _make_channel(tmp_path, monkeypatch, raw, uid=uid)
        channel._store.load()
        digests.append(channel._resolve_thread(channel._fetch_new_messages()[0]))

    sent: list = []
    monkeypatch.setattr(channel, "_smtp_send", lambda m: sent.append(m))
    await channel.send(OutboundMessage(
        channel="email", chat_id="alice@example.com", content="re A",
        metadata={"email": {"thread": digests[0]}},
    ))
    assert sent[0]["In-Reply-To"] == "<a1@x>"
    assert sent[0]["Subject"] == "Re: Thread A"


@pytest.mark.asyncio
async def test_send_without_thread_uses_latest_for_address(tmp_path, monkeypatch) -> None:
    raw = _make_raw_email_threaded(message_id="<m1@x>", subject="Question")
    channel, _ = _make_channel(tmp_path, monkeypatch, raw, threading_mode="sender")
    channel._store.load()
    channel._resolve_thread(channel._fetch_new_messages()[0])

    sent: list = []
    monkeypatch.setattr(channel, "_smtp_send", lambda m: sent.append(m))
    await channel.send(OutboundMessage(
        channel="email", chat_id="alice@example.com", content="hi",
    ))
    assert sent[0]["In-Reply-To"] == "<m1@x>"
    assert sent[0]["Subject"] == "Re: Question"


@pytest.mark.asyncio
async def test_send_message_id_domain_ignores_display_name(monkeypatch) -> None:
    """From with a display name must not leak into the Message-ID domain."""
    channel = EmailChannel(
        _make_config(from_address="Durin Bot <bot@example.com>"), MessageBus()
    )
    sent: list = []
    monkeypatch.setattr(channel, "_smtp_send", lambda m: sent.append(m))

    await channel.send(OutboundMessage(
        channel="email", chat_id="alice@example.com", content="hi",
    ))

    assert re.match(r"^<[^<>@]+@example\.com>$", sent[0]["Message-ID"])


@pytest.mark.asyncio
async def test_send_skips_thread_index_when_store_entry_corrupt(tmp_path, monkeypatch) -> None:
    """A corrupt thread_index_conv_id must not kill the send; drop the header."""
    from durin.channels.email_threads import thread_digest

    channel = EmailChannel(_make_config(), MessageBus())
    channel._store.upsert_inbound(
        thread_digest("<m1@example.com>"),
        root="<m1@example.com>",
        address="alice@example.com",
        subject="Invoice",
        references=[],
        message_id="<m1@example.com>",
        thread_index_conv_id="zz-not-hex",
    )
    sent: list = []
    monkeypatch.setattr(channel, "_smtp_send", lambda m: sent.append(m))

    await channel.send(OutboundMessage(
        channel="email", chat_id="alice@example.com", content="Acknowledged.",
    ))

    assert len(sent) == 1
    assert "Thread-Index" not in sent[0]


@pytest.mark.asyncio
async def test_send_falls_back_to_references_tail_when_last_message_id_empty(monkeypatch) -> None:
    """When last_message_id is empty but references exist, In-Reply-To must
    fall back to the chain's last element instead of dropping stitching."""
    from durin.channels.email_threads import thread_digest

    channel = EmailChannel(_make_config(), MessageBus())
    digest = thread_digest("<m1@example.com>")
    channel._store.upsert_inbound(
        digest,
        root="<m1@example.com>",
        address="alice@example.com",
        subject="Invoice",
        references=["<m1@example.com>", "<m2@example.com>"],
        message_id="",
    )
    entry = channel._store.get(digest)
    entry["last_message_id"] = ""

    sent: list = []
    monkeypatch.setattr(channel, "_smtp_send", lambda m: sent.append(m))
    await channel.send(OutboundMessage(
        channel="email", chat_id="alice@example.com", content="On it.",
        metadata={"email": {"thread": digest}},
    ))

    msg = sent[0]
    assert msg["In-Reply-To"] == "<m2@example.com>"
    refs = msg["References"].split()
    assert refs == ["<m1@example.com>", "<m2@example.com>"]
    assert refs[-1] == msg["In-Reply-To"]


@pytest.mark.asyncio
async def test_send_builds_html_multipart(tmp_path, monkeypatch) -> None:
    raw = _make_raw_email_threaded(message_id="<m1@x>", subject="Q")
    channel, _ = _make_channel(tmp_path, monkeypatch, raw)
    channel._store.load()
    channel._resolve_thread(channel._fetch_new_messages()[0])

    sent: list = []
    monkeypatch.setattr(channel, "_smtp_send", lambda m: sent.append(m))
    await channel.send(OutboundMessage(
        channel="email", chat_id="alice@example.com",
        content="**bold** and a [link](https://example.com)",
    ))
    msg = sent[0]
    assert msg.get_content_type() == "multipart/alternative"
    plain = msg.get_body(preferencelist=("plain",)).get_content()
    html_part = msg.get_body(preferencelist=("html",)).get_content()
    assert "**bold**" in plain
    assert "<strong>bold</strong>" in html_part
    assert '<a href="https://example.com">' in html_part


@pytest.mark.asyncio
async def test_send_appends_copy_to_sent_folder(tmp_path, monkeypatch) -> None:
    raw = _make_raw_email_threaded(message_id="<m1@x>", subject="Q")
    channel, _ = _make_channel(tmp_path, monkeypatch, raw)
    channel._store.load()
    channel._resolve_thread(channel._fetch_new_messages()[0])

    appended: list = []

    class FakeIMAPSent:
        def login(self, _u, _p):
            return "OK", [b"ok"]

        def list(self):
            return "OK", [b'(\\HasNoChildren \\Sent) "/" "Enviados"']

        def append(self, folder, flags, _dt, body):
            appended.append((folder, flags, body))
            return "OK", [b""]

        def logout(self):
            return "BYE", [b""]

    monkeypatch.setattr(
        "durin.channels.email.imaplib.IMAP4_SSL", lambda _h, _p, **_kw: FakeIMAPSent()
    )
    monkeypatch.setattr(channel, "_smtp_send", lambda m: None)
    await channel.send(OutboundMessage(
        channel="email", chat_id="alice@example.com", content="hi",
    ))
    assert len(appended) == 1
    assert appended[0][0] == '"Enviados"'


@pytest.mark.asyncio
async def test_send_appends_copy_to_sent_folder_unquoted_name(tmp_path, monkeypatch) -> None:
    raw = _make_raw_email_threaded(message_id="<m1@x>", subject="Q")
    channel, _ = _make_channel(tmp_path, monkeypatch, raw)
    channel._store.load()
    channel._resolve_thread(channel._fetch_new_messages()[0])

    appended: list = []

    class FakeIMAPSent:
        def login(self, _u, _p):
            return "OK", [b"ok"]

        def list(self):
            return "OK", [b'(\\HasNoChildren \\Sent) "/" Sent-Items']

        def append(self, folder, flags, _dt, body):
            appended.append((folder, flags, body))
            return "OK", [b""]

        def logout(self):
            return "BYE", [b""]

    monkeypatch.setattr(
        "durin.channels.email.imaplib.IMAP4_SSL", lambda _h, _p, **_kw: FakeIMAPSent()
    )
    monkeypatch.setattr(channel, "_smtp_send", lambda m: None)
    await channel.send(OutboundMessage(
        channel="email", chat_id="alice@example.com", content="hi",
    ))
    assert len(appended) == 1
    assert appended[0][0] == "Sent-Items"


@pytest.mark.asyncio
async def test_sent_folder_detection_not_cached_on_failure(tmp_path, monkeypatch) -> None:
    raw = _make_raw_email_threaded(message_id="<m1@x>", subject="Q")
    channel, _ = _make_channel(tmp_path, monkeypatch, raw)
    channel._store.load()
    channel._resolve_thread(channel._fetch_new_messages()[0])

    appended: list = []

    class FakeIMAPListFails:
        def login(self, _u, _p):
            return "OK", [b"ok"]

        def list(self):
            raise OSError("list failed")

        def append(self, folder, flags, _dt, body):
            appended.append((folder, flags, body))
            return "OK", [b""]

        def logout(self):
            return "BYE", [b""]

    class FakeIMAPListWorks:
        def login(self, _u, _p):
            return "OK", [b"ok"]

        def list(self):
            return "OK", [b'(\\Sent) "/" "Enviados"']

        def append(self, folder, flags, _dt, body):
            appended.append((folder, flags, body))
            return "OK", [b""]

        def logout(self):
            return "BYE", [b""]

    monkeypatch.setattr(
        "durin.channels.email.imaplib.IMAP4_SSL", lambda _h, _p, **_kw: FakeIMAPListFails()
    )
    monkeypatch.setattr(channel, "_smtp_send", lambda m: None)
    await channel.send(OutboundMessage(
        channel="email", chat_id="alice@example.com", content="hi",
    ))
    assert appended[0][0] == '"Sent"'
    assert channel._sent_folder is None

    monkeypatch.setattr(
        "durin.channels.email.imaplib.IMAP4_SSL", lambda _h, _p, **_kw: FakeIMAPListWorks()
    )
    await channel.send(OutboundMessage(
        channel="email", chat_id="alice@example.com", content="hi again",
    ))
    assert appended[1][0] == '"Enviados"'
    assert channel._sent_folder == '"Enviados"'


@pytest.mark.asyncio
async def test_sent_append_failure_does_not_break_send(tmp_path, monkeypatch) -> None:
    raw = _make_raw_email_threaded(message_id="<m1@x>", subject="Q")
    channel, _ = _make_channel(tmp_path, monkeypatch, raw)
    channel._store.load()
    channel._resolve_thread(channel._fetch_new_messages()[0])

    def _boom(_h, _p, **_kw):
        raise OSError("imap down")

    monkeypatch.setattr("durin.channels.email.imaplib.IMAP4_SSL", _boom)
    sent: list = []
    monkeypatch.setattr(channel, "_smtp_send", lambda m: sent.append(m))
    await channel.send(OutboundMessage(
        channel="email", chat_id="alice@example.com", content="hi",
    ))
    assert len(sent) == 1  # send succeeded despite append failure


# ---------------------------------------------------------------------------
# Regression: ordinary replies must carry the thread digest (Fix 1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_ordinary_reply_uses_embedded_thread_not_latest_for_address(
    tmp_path, monkeypatch
) -> None:
    """Reproduces the original bug shape: without the start()-loop embedding an
    explicit `email.thread` digest into inbound metadata, an ordinary reply's
    metadata (passed through unchanged by _assemble_outbound) would carry no
    digest at all, so send() would fall back to latest_for_address and stitch
    the reply into whichever thread for that sender is most recent — not
    necessarily the one being replied to."""
    import time

    raw_a = _make_raw_email_threaded(message_id="<a1@x>", subject="Thread A")
    channel, _ = _make_channel(tmp_path, monkeypatch, raw_a, uid="1")
    channel._store.load()
    item_a = channel._fetch_new_messages()[0]
    digest_a = channel._resolve_thread(item_a)
    # What EmailChannel.start() now does before _handle_message.
    item_a["metadata"]["email"] = {"thread": digest_a}
    channel._store._threads[digest_a]["last_seen"] = time.time() - 100  # make A older

    raw_b = _make_raw_email_threaded(message_id="<b1@x>", subject="Thread B")
    monkeypatch.setattr(
        "durin.channels.email.imaplib.IMAP4_SSL",
        lambda _h, _p, **_kw: _FakeIMAPSingle(raw_b, uid="2"),
    )
    item_b = channel._fetch_new_messages()[0]
    digest_b = channel._resolve_thread(item_b)
    item_b["metadata"]["email"] = {"thread": digest_b}
    assert digest_b != digest_a

    sent: list = []
    monkeypatch.setattr(channel, "_smtp_send", lambda m: sent.append(m))
    await channel.send(OutboundMessage(
        channel="email",
        chat_id="alice@example.com",
        content="Reply to A.",
        metadata=item_a["metadata"],
    ))

    assert sent[0]["In-Reply-To"] == "<a1@x>"


# ---------------------------------------------------------------------------
# Fix 2: Thread-Index fallback reachable even when References resolved to a
# root the store doesn't recognize (rewritten References).
# ---------------------------------------------------------------------------


def test_resolve_thread_index_recovers_when_references_rewritten(tmp_path, monkeypatch) -> None:
    import base64

    from durin.channels.email_threads import thread_digest

    conv = base64.b64encode(bytes(range(22))).decode()
    first = _make_raw_email_threaded(
        message_id="<m1@example.com>", subject="Budget", thread_index=conv
    )
    channel, _ = _make_channel(tmp_path, monkeypatch, first)
    d1 = channel._resolve_thread(channel._fetch_new_messages()[0])

    # References got rewritten to an unknown root, but the Thread-Index
    # conversation prefix and normalized subject still match.
    rewritten = _make_raw_email_threaded(
        message_id="<m2@example.com>", subject="RE: Budget",
        references="<rewritten@x>", thread_index=conv,
    )
    channel2, _ = _make_channel(tmp_path, monkeypatch, rewritten, uid="124")
    d2 = channel2._resolve_thread(channel2._fetch_new_messages()[0])
    assert d2 == d1
    assert channel2._store.get(d1)["root"] == "<m1@example.com>"

    # Unknown References AND no conv match → a genuinely new thread, rooted at
    # the References-derived value (kept, not discarded).
    unrelated = _make_raw_email_threaded(
        message_id="<m3@example.com>", subject="Something else",
        references="<unknown@x>",
    )
    channel3, _ = _make_channel(tmp_path, monkeypatch, unrelated, uid="125")
    d3 = channel3._resolve_thread(channel3._fetch_new_messages()[0])
    assert d3 != d1
    assert d3 == thread_digest("<unknown@x>")


# ---------------------------------------------------------------------------
# Fix 4: an explicit but unknown digest must not fall back to latest_for_address
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_explicit_digest_miss_does_not_fall_back_to_latest(
    tmp_path, monkeypatch
) -> None:
    raw = _make_raw_email_threaded(message_id="<m1@x>", subject="Known thread")
    channel, _ = _make_channel(tmp_path, monkeypatch, raw)
    channel._store.load()
    channel._resolve_thread(channel._fetch_new_messages()[0])

    sent: list = []
    monkeypatch.setattr(channel, "_smtp_send", lambda m: sent.append(m))
    await channel.send(OutboundMessage(
        channel="email", chat_id="alice@example.com", content="hi",
        metadata={"email": {"thread": "0000000000000000"}},
    ))
    assert "In-Reply-To" not in sent[0]


@pytest.mark.asyncio
async def test_send_ignores_top_level_subject_passthrough(tmp_path, monkeypatch) -> None:
    """Ordinary agent replies pass INBOUND metadata through to send() unchanged,
    and that metadata carries a top-level "subject" key holding the inbound
    mail's own subject. That key must not be read as an override — only
    metadata["email"]["subject"] is an intentional override — otherwise every
    ordinary reply would echo the inbound subject verbatim instead of getting
    the "Re: " prefix."""
    from durin.channels.email_threads import thread_digest

    raw = _make_raw_email_threaded(message_id="<m1@x>", subject="Invoice")
    channel, _ = _make_channel(tmp_path, monkeypatch, raw)
    channel._store.load()
    channel._resolve_thread(channel._fetch_new_messages()[0])
    digest = thread_digest("<m1@x>")

    sent: list = []
    monkeypatch.setattr(channel, "_smtp_send", lambda m: sent.append(m))
    await channel.send(OutboundMessage(
        channel="email",
        chat_id="alice@example.com",
        content="hi",
        metadata={
            "message_id": "<m1@x>",
            "subject": "Invoice",
            "uid": "123",
            "email": {"thread": digest},
        },
    ))
    assert sent[0]["Subject"] == "Re: Invoice"


# ---------------------------------------------------------------------------
# Fix 5: CRLF injection via RFC 2047-decoded headers
# ---------------------------------------------------------------------------


def test_decode_header_value_sanitizes_embedded_crlf() -> None:
    """RFC 2047 decoding can smuggle CR/LF via a crafted encoded-word; the
    decoded value must be collapsed to a single line so it can't inject
    headers into an outbound reply."""
    import base64

    encoded = base64.b64encode("Hi\r\nEvil-Header: x".encode()).decode()
    raw_header = f"=?utf-8?b?{encoded}?="
    decoded = EmailChannel._decode_header_value(raw_header)
    assert "\r" not in decoded
    assert "\n" not in decoded
    assert decoded == "Hi Evil-Header: x"


@pytest.mark.asyncio
async def test_inbound_crlf_subject_is_sanitized_and_reply_succeeds(
    tmp_path, monkeypatch
) -> None:
    """A CRLF-smuggling encoded-word Subject must parse inbound cleanly and,
    critically, must not reach an outbound header unsanitized — stdlib's
    email package raises ValueError on an embedded newline, which would
    permanently fail the reply."""
    import base64

    encoded = base64.b64encode("Hi\r\nEvil-Header: x".encode()).decode()
    raw_subject = f"=?utf-8?b?{encoded}?="
    msg = EmailMessage()
    msg["From"] = "alice@example.com"
    msg["To"] = "bot@example.com"
    msg["Subject"] = raw_subject
    msg["Message-ID"] = "<crlf@example.com>"
    msg.set_content("body")
    raw = msg.as_bytes()

    channel, _ = _make_channel(tmp_path, monkeypatch, raw)
    item = channel._fetch_new_messages()[0]
    assert "\r" not in item["subject"]
    assert "\n" not in item["subject"]

    channel._resolve_thread(item)
    sent: list = []
    monkeypatch.setattr(channel, "_smtp_send", lambda m: sent.append(m))
    await channel.send(OutboundMessage(
        channel="email", chat_id="alice@example.com", content="Ack.",
    ))
    subject = sent[0]["Subject"]
    assert "\r" not in subject
    assert "\n" not in subject
