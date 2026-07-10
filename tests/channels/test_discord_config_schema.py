from durin.channels.discord import DiscordChannel, DiscordConfig


def _extra(m, f):
    return m.model_fields[f].json_schema_extra or {}


def test_discord_exposes_config_model():
    assert DiscordChannel.config_model() is DiscordConfig


def test_token_marked_secret_and_required():
    assert _extra(DiscordConfig, "token").get("secret") is True
    assert _extra(DiscordConfig, "token").get("required") is True


def test_proxy_password_marked_secret():
    assert _extra(DiscordConfig, "proxy_password").get("secret") is True
    # the companion username is not a credential on its own
    assert _extra(DiscordConfig, "proxy_username").get("secret") is not True


def test_access_group_holds_who_and_where():
    for field in ("allow_from", "allow_channels", "group_policy"):
        assert _extra(DiscordConfig, field).get("group") == "access", field


def test_behavior_and_security_groups():
    for field in ("streaming", "read_receipt_emoji", "working_emoji", "working_emoji_delay"):
        assert _extra(DiscordConfig, field).get("group") == "behavior", field
    for field in ("proxy", "proxy_username", "proxy_password"):
        assert _extra(DiscordConfig, field).get("group") == "security", field


def test_intents_excluded_from_ui_schema():
    """intents is a raw gateway bitfield: a mistyped digit breaks the bot
    silently, so it carries no group and never reaches the form."""
    from durin.service.config import _channel_field_schema

    names = {f["name"] for f in _channel_field_schema(DiscordConfig)}
    assert "intents" not in names


def test_ui_schema_field_set_is_exactly_what_we_surface():
    from durin.service.config import _channel_field_schema

    names = {f["name"] for f in _channel_field_schema(DiscordConfig)}
    assert names == {
        "token",
        "allow_from",
        "allow_channels",
        "group_policy",
        "streaming",
        "read_receipt_emoji",
        "working_emoji",
        "working_emoji_delay",
        "proxy",
        "proxy_username",
        "proxy_password",
    }


def test_group_policy_renders_as_select_with_both_options():
    from durin.service.config import _channel_field_schema

    field = next(f for f in _channel_field_schema(DiscordConfig) if f["name"] == "group_policy")
    assert field["type"] == "select"
    assert set(field["options"]) == {"mention", "open"}


def test_allow_channels_renders_as_string_list():
    from durin.service.config import _channel_field_schema

    field = next(f for f in _channel_field_schema(DiscordConfig) if f["name"] == "allow_channels")
    assert field["type"] == "string_list"


def test_token_renders_as_secret_field():
    from durin.service.config import _channel_field_schema

    field = next(f for f in _channel_field_schema(DiscordConfig) if f["name"] == "token")
    assert field["type"] == "secret"




def test_working_emoji_delay_renders_as_a_number_not_a_text_box():
    """A float knob must offer a numeric input; a plain text box invites
    locale-specific decimal separators that fail validation opaquely."""
    from durin.service.config import _channel_field_schema

    field = next(
        f for f in _channel_field_schema(DiscordConfig) if f["name"] == "working_emoji_delay"
    )
    assert field["type"] == "float"


def test_discord_does_not_get_a_legacy_credential_field():
    """channels_list only computes credential_field when a channel exposes no
    fields; with the typed form in place the legacy password box must not
    render for discord."""
    from durin.service.config import _CRED_FIELDS, _channel_field_schema

    fields = _channel_field_schema(DiscordConfig)
    credential_field = None
    if not fields:  # the exact branch channels_list takes
        defaults = DiscordChannel.default_config()
        credential_field = next((n for n in _CRED_FIELDS if n in defaults), None)
    assert credential_field is None


def test_intents_is_not_a_config_key_at_all():
    """A raw gateway bitfield has no safe home in any tier: the guided flow
    cannot verify it, a form cannot render it, and a mistyped digit yields a
    bot that connects and silently ignores messages. durin derives it."""
    assert "intents" not in DiscordConfig.model_fields
    assert "intents" not in DiscordChannel.default_config()


def test_derived_intents_cover_exactly_the_events_the_adapter_handles():
    from durin.channels.discord import GATEWAY_INTENTS

    assert GATEWAY_INTENTS.guilds is True  # thread create/update/delete, channel cache
    assert GATEWAY_INTENTS.guild_messages is True  # on_message in servers
    assert GATEWAY_INTENTS.dm_messages is True  # on_message in DMs
    assert GATEWAY_INTENTS.message_content is True  # privileged: read the text
    # Nothing durin does needs these, and each is a privileged ask or noise.
    assert GATEWAY_INTENTS.members is False
    assert GATEWAY_INTENTS.presences is False
    assert GATEWAY_INTENTS.voice_states is False
    assert GATEWAY_INTENTS.guild_reactions is False


def test_derived_value_matches_the_former_default():
    """Nobody on defaults sees a behaviour change from dropping the knob."""
    from durin.channels.discord import GATEWAY_INTENTS

    assert GATEWAY_INTENTS.value == 37377


def test_a_legacy_intents_key_is_ignored_loudly():
    """Pydantic drops unknown keys silently. An operator who hand-set intents
    must be told their value no longer does anything."""
    from loguru import logger as loguru_logger

    from durin.bus.queue import MessageBus

    warnings: list[str] = []
    handler_id = loguru_logger.add(lambda m: warnings.append(str(m)), level="WARNING", format="{message}")
    try:
        DiscordChannel({"enabled": True, "token": "t", "intents": 12345}, MessageBus())
    finally:
        loguru_logger.remove(handler_id)

    assert any("intents" in w and "obsolete" in w for w in warnings), warnings


def test_a_config_without_the_legacy_key_warns_about_nothing():
    from loguru import logger as loguru_logger

    from durin.bus.queue import MessageBus

    warnings: list[str] = []
    handler_id = loguru_logger.add(lambda m: warnings.append(str(m)), level="WARNING", format="{message}")
    try:
        DiscordChannel({"enabled": True, "token": "t"}, MessageBus())
    finally:
        loguru_logger.remove(handler_id)

    assert warnings == []
