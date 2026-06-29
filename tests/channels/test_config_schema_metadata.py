from durin.channels.email import EmailChannel, EmailConfig
from durin.channels.websocket import WebSocketChannel, WebSocketConfig


def _extra(model, field):
    return model.model_fields[field].json_schema_extra or {}


def test_email_channel_exposes_config_model():
    assert EmailChannel.config_model() is EmailConfig


def test_websocket_channel_exposes_config_model():
    assert WebSocketChannel.config_model() is WebSocketConfig


def test_base_channel_config_model_is_none():
    from durin.channels.base import BaseChannel
    assert BaseChannel.config_model() is None


def test_email_passwords_marked_secret():
    assert _extra(EmailConfig, "imap_password").get("secret") is True
    assert _extra(EmailConfig, "smtp_password").get("secret") is True
    # non-secret field stays unmarked
    assert _extra(EmailConfig, "imap_host").get("secret") is not True


def test_email_fields_grouped():
    assert _extra(EmailConfig, "imap_host").get("group") == "imap"
    assert _extra(EmailConfig, "smtp_host").get("group") == "smtp"
    assert _extra(EmailConfig, "auto_reply_enabled").get("group") == "behavior"
    assert _extra(EmailConfig, "verify_dkim").get("group") == "security"
    assert _extra(EmailConfig, "max_attachment_size").get("group") == "attachments"
    assert _extra(EmailConfig, "consent_granted").get("group") == "access"
    assert _extra(EmailConfig, "allow_from").get("group") == "access"


def test_websocket_token_marked_secret():
    assert _extra(WebSocketConfig, "token").get("secret") is True
