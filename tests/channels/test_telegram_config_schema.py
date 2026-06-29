from durin.channels.telegram import TelegramChannel, TelegramConfig


def _extra(m, f):
    return m.model_fields[f].json_schema_extra or {}


def test_telegram_exposes_config_model():
    assert TelegramChannel.config_model() is TelegramConfig


def test_telegram_token_secret_and_allow_from_access():
    assert _extra(TelegramConfig, "token").get("secret") is True
    assert _extra(TelegramConfig, "allow_from").get("group") == "access"


def test_telegram_schema_only_token_and_allow_from():
    from durin.service.config import _channel_field_schema
    names = {f["name"] for f in _channel_field_schema(TelegramConfig)}
    assert names == {"token", "allow_from"}
