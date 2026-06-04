"""Tests for ``format_provider_error_content`` — readable provider errors.

Regression guard for the UX bug where the raw Python ``repr`` of an error
dict leaked to the user (``Error: {'code': '1210', 'message': '…'}``).
"""

from durin.providers.base import format_provider_error_content


def test_dict_with_message_and_code():
    body = {"code": "1210", "message": "Invalid API parameter, please check the documentation."}
    assert (
        format_provider_error_content(body)
        == "Error (1210): Invalid API parameter, please check the documentation."
    )


def test_nested_openai_error_shape():
    body = {"error": {"message": "Rate limit exceeded", "code": "rate_limit"}}
    assert format_provider_error_content(body) == "Error (rate_limit): Rate limit exceeded"


def test_message_without_code():
    body = {"message": "余额不足或无可用资源包,请充值。"}
    assert format_provider_error_content(body) == "Error: 余额不足或无可用资源包,请充值。"


def test_json_string_is_parsed():
    body = '{"code": "1113", "message": "insufficient balance"}'
    assert format_provider_error_content(body) == "Error (1113): insufficient balance"


def test_plain_string_passthrough_trimmed():
    body = "  upstream connect error  "
    assert format_provider_error_content(body) == "Error: upstream connect error"


def test_no_message_falls_back_to_repr():
    body = {"unexpected": "shape"}
    out = format_provider_error_content(body)
    assert out.startswith("Error: ")
    assert "unexpected" in out


def test_empty_body_uses_exception():
    exc = RuntimeError("connection refused")
    assert format_provider_error_content(None, exc) == "Error calling LLM: connection refused"
    assert format_provider_error_content("", exc) == "Error calling LLM: connection refused"


def test_output_keeps_error_prefix_for_webui_detection():
    # The webui keys off a leading "Error" to style the turn as an error card.
    for body in [{"message": "x"}, "raw text", {"error": {"message": "y"}}]:
        assert format_provider_error_content(body).startswith("Error")
