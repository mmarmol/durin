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
