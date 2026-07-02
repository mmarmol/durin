"""Azure OpenAI provider using the OpenAI SDK Responses API.

Uses ``AsyncOpenAI`` pointed at ``https://{endpoint}/openai/v1/`` which
routes to the Responses API (``/responses``).  Reuses shared conversion
helpers from :mod:`durin.providers.openai_responses`.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from openai import AsyncOpenAI

from durin.providers.base import LLMProvider, LLMResponse, format_provider_error_content
from durin.providers.openai_responses import (
    consume_sdk_stream,
    convert_messages,
    convert_tools,
    parse_response_output,
)


class AzureOpenAIProvider(LLMProvider):
    """Azure OpenAI provider backed by the Responses API.

    Features:
    - Uses the OpenAI Python SDK (``AsyncOpenAI``) with
      ``base_url = {endpoint}/openai/v1/``
    - Calls ``client.responses.create()`` (Responses API)
    - Reuses shared message/tool/SSE conversion from
      ``openai_responses``
    """

    def __init__(
        self,
        api_key: str = "",
        api_base: str = "",
        default_model: str = "gpt-5.2-chat",
    ):
        super().__init__(api_key, api_base)
        self.default_model = default_model

        if not api_key:
            raise ValueError("Azure OpenAI api_key is required")
        if not api_base:
            raise ValueError("Azure OpenAI api_base is required")

        # Normalise: ensure trailing slash
        if not api_base.endswith("/"):
            api_base += "/"
        self.api_base = api_base

        # SDK client targeting the Azure Responses API endpoint
        base_url = f"{api_base.rstrip('/')}/openai/v1/"
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            default_headers={"x-session-affinity": uuid.uuid4().hex},
            max_retries=0,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _supports_temperature(
        deployment_name: str,
        reasoning_effort: str | None = None,
    ) -> bool:
        """Return True when temperature is likely supported for this deployment."""
        if reasoning_effort and reasoning_effort.lower() != "none":
            return False
        name = deployment_name.lower()
        return not any(token in name for token in ("gpt-5", "o1", "o3", "o4"))

    def _build_body(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str | None,
        max_tokens: int,
        temperature: float,
        reasoning_effort: str | None,
        tool_choice: str | dict[str, Any] | None,
        top_p: float | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build the Responses API request body from Chat-Completions-style args."""
        deployment = model or self.default_model
        instructions, input_items = convert_messages(self._sanitize_empty_content(messages))

        body: dict[str, Any] = {
            "model": deployment,
            "instructions": instructions or None,
            "input": input_items,
            "max_output_tokens": max(1, max_tokens),
            "store": False,
            "stream": False,
        }

        if self._supports_temperature(deployment, reasoning_effort):
            body["temperature"] = temperature

        if reasoning_effort and reasoning_effort.lower() != "none":
            body["reasoning"] = {"effort": reasoning_effort}
            body["include"] = ["reasoning.encrypted_content"]

        if tools:
            body["tools"] = convert_tools(tools)
            body["tool_choice"] = tool_choice or "auto"

        if top_p is not None:
            body["top_p"] = top_p

        if extra_body:
            body["extra_body"] = extra_body

        return body

    @staticmethod
    def _handle_error(e: Exception) -> LLMResponse:
        response = getattr(e, "response", None)
        body = getattr(e, "body", None) or getattr(response, "text", None)
        msg = format_provider_error_content(body, e)
        retry_after = LLMProvider._extract_retry_after_from_headers(getattr(response, "headers", None))
        if retry_after is None:
            retry_after = LLMProvider._extract_retry_after(msg)
        return LLMResponse(content=msg, finish_reason="error", retry_after=retry_after)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        top_p: float | None = None,
        # Accepted for retry-wrapper signature compatibility; not wired to this backend yet.
        top_k: int | None = None,
        repeat_penalty: float | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> LLMResponse:
        body = self._build_body(
            messages, tools, model, max_tokens, temperature,
            reasoning_effort, tool_choice,
            top_p=top_p, extra_body=extra_body,
        )
        try:
            response = await self._client.responses.create(**body)
            return parse_response_output(response)
        except Exception as e:
            return self._handle_error(e)

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        on_content_delta: Callable[[str], Awaitable[None]] | None = None,
        on_thinking_delta: Callable[[str], Awaitable[None]] | None = None,
        top_p: float | None = None,
        # Accepted for retry-wrapper signature compatibility; not wired to this backend yet.
        top_k: int | None = None,
        repeat_penalty: float | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> LLMResponse:
        _ = on_thinking_delta
        body = self._build_body(
            messages, tools, model, max_tokens, temperature,
            reasoning_effort, tool_choice,
            top_p=top_p, extra_body=extra_body,
        )
        body["stream"] = True

        try:
            stream = await self._client.responses.create(**body)
            content, tool_calls, finish_reason, usage, reasoning_content = (
                await consume_sdk_stream(stream, on_content_delta)
            )
            return LLMResponse(
                content=content or None,
                tool_calls=tool_calls,
                finish_reason=finish_reason,
                usage=usage,
                reasoning_content=reasoning_content,
            )
        except Exception as e:
            return self._handle_error(e)

    def get_default_model(self) -> str:
        return self.default_model
