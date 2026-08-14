"""GatewayConfig fields backing the gateway's HTTP surfaces."""

from __future__ import annotations

from durin.config.schema import GatewayConfig


def test_gateway_api_request_timeout_default_and_alias() -> None:
    assert GatewayConfig().api_request_timeout == 120.0
    parsed = GatewayConfig.model_validate({"apiRequestTimeout": 300})
    assert parsed.api_request_timeout == 300.0


def test_gateway_api_request_timeout_serializes_camel_case() -> None:
    dumped = GatewayConfig().model_dump(by_alias=True)
    assert dumped["apiRequestTimeout"] == 120.0
