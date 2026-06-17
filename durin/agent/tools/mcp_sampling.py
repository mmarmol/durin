"""MCP server→client capability helpers (SP-6).

6a/6b: RFC-5424 log-level mapping (consumed by MCPServerConnection).
6c:    RpmLimiter, MCP↔durin message translators, SamplingGovernance,
       SamplingRunner — the testable sampling unit; no transport needed.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

# ---------------------------------------------------------------------------
# 6b — log level mapping
# ---------------------------------------------------------------------------

_MCP_TO_LOGURU: dict[str, str] = {
    "debug": "DEBUG",
    "info": "INFO",
    "notice": "INFO",
    "warning": "WARNING",
    "error": "ERROR",
    "critical": "CRITICAL",
    "alert": "CRITICAL",
    "emergency": "CRITICAL",
}


def mcp_log_level_to_loguru(level: str) -> str:
    """Map an RFC-5424 MCP logging level to a loguru level name."""
    return _MCP_TO_LOGURU.get(level, "INFO")


# ---------------------------------------------------------------------------
# 6c — RPM limiter
# ---------------------------------------------------------------------------


class RpmLimiter:
    """Sliding-window requests-per-minute limiter via a 60s timestamp deque.

    ``now`` is injectable for deterministic tests.
    """

    def __init__(self, rpm: int, now: Callable[[], float] = time.monotonic) -> None:
        self._rpm = max(0, int(rpm))
        self._now = now
        self._hits: deque[float] = deque()

    def allow(self) -> bool:
        if self._rpm <= 0:
            return False
        t = self._now()
        cutoff = t - 60.0
        while self._hits and self._hits[0] < cutoff:
            self._hits.popleft()
        if len(self._hits) >= self._rpm:
            return False
        self._hits.append(t)
        return True


# ---------------------------------------------------------------------------
# 6c — MCP↔durin message and tool translators
# ---------------------------------------------------------------------------


def _content_to_openai_block(content: Any) -> dict[str, Any]:
    ctype = getattr(content, "type", None)
    if ctype == "text":
        return {"type": "text", "text": content.text}
    if ctype in ("image", "audio"):
        mime = content.mimeType
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{content.data}"},
        }
    # tool_use / tool_result inside a prior turn — represent as text so the
    # provider still sees the gist (durin's chat API is OpenAI-shaped; a full
    # native tool-result round-trip is the server's job, not ours to reify).
    return {"type": "text", "text": str(content)}


def sampling_messages_to_openai(
    messages: list[Any], system_prompt: str | None
) -> list[dict[str, Any]]:
    """Translate MCP SamplingMessage list + system prompt to OpenAI-style messages."""
    out: list[dict[str, Any]] = []
    if system_prompt:
        out.append({"role": "system", "content": system_prompt})
    for m in messages:
        content = m.content
        if isinstance(content, list):
            blocks = [_content_to_openai_block(c) for c in content]
            out.append({"role": m.role, "content": blocks})
        elif getattr(content, "type", None) == "text":
            out.append({"role": m.role, "content": content.text})
        else:
            out.append({"role": m.role, "content": [_content_to_openai_block(content)]})
    return out


def mcp_tools_to_openai(tools: list[Any] | None) -> list[dict[str, Any]] | None:
    """Translate MCP Tool list to OpenAI-style function definitions."""
    if not tools:
        return None
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description or "",
                "parameters": t.inputSchema or {"type": "object", "properties": {}},
            },
        }
        for t in tools
    ]


def llm_response_to_sampling_result(response: Any, model: str, emit_tools: bool) -> Any:
    """Map an LLMResponse to a CreateMessageResult or CreateMessageResultWithTools.

    When ``emit_tools`` is True and the response carries tool calls, returns a
    CreateMessageResultWithTools with ToolUseContent blocks (the server runs the
    tools and calls back). Otherwise returns a plain text CreateMessageResult.
    """
    import mcp.types as types

    if emit_tools and response.tool_calls:
        blocks: list[Any] = [
            types.ToolUseContent(
                type="tool_use", id=tc.id, name=tc.name, input=tc.arguments or {}
            )
            for tc in response.tool_calls
        ]
        text = (response.content or "").strip()
        if text:
            blocks.insert(0, types.TextContent(type="text", text=text))
        content = blocks if len(blocks) > 1 else blocks[0]
        return types.CreateMessageResultWithTools(
            role="assistant",
            content=content,
            model=model,
            stopReason="toolUse",
        )
    return types.CreateMessageResult(
        role="assistant",
        content=types.TextContent(type="text", text=(response.content or "")),
        model=model,
        stopReason="endTurn",
    )


# ---------------------------------------------------------------------------
# 6c — SamplingGovernance + SamplingRunner
# ---------------------------------------------------------------------------


@dataclass
class SamplingGovernance:
    """Governance knobs for one server's sampling access."""

    max_tokens_cap: int = 4096
    requests_per_minute: int = 10
    allowed_models: list[str] = field(default_factory=list)
    allow_tools: bool = True
    max_tool_rounds: int = 4

    @classmethod
    def from_config(cls, cfg: Any) -> "SamplingGovernance":
        return cls(
            max_tokens_cap=cfg.max_tokens_cap,
            requests_per_minute=cfg.requests_per_minute,
            allowed_models=list(cfg.allowed_models),
            allow_tools=cfg.allow_tools,
            max_tool_rounds=cfg.max_tool_rounds,
        )


class SamplingRunner:
    """Routes one server-initiated sampling request through durin's provider
    under governance. ``run`` returns a CreateMessageResult(WithTools) or
    ErrorData (the clean MCP rejection path).

    The tool-round counter is scoped to this runner instance (= one server
    connection). Because the SDK gives no conversation id on
    CreateMessageRequestParams, the counter is a conservative per-connection
    upper bound — it errs toward stopping tool loops sooner rather than later.
    """

    def __init__(
        self,
        provider: Any,
        default_model: str,
        governance: SamplingGovernance,
    ) -> None:
        self.provider = provider
        self.default_model = default_model
        self.governance = governance
        self._limiter = RpmLimiter(governance.requests_per_minute)
        self._tool_rounds = 0

    def _resolve_model(self, params: Any) -> tuple[str | None, Any]:
        """Return (model_name, None) or (None, ErrorData) for a model-whitelist violation."""
        import mcp.types as types

        gov = self.governance
        requested: str | None = None
        prefs = getattr(params, "modelPreferences", None)
        if prefs is not None and getattr(prefs, "hints", None):
            requested = next((h.name for h in prefs.hints if h.name), None)

        model = requested or self.default_model

        if gov.allowed_models:
            # The operator's own default_model is always permitted; allowed_models
            # restricts SERVER-requested overrides, not the operator's own default.
            if model != self.default_model and model not in gov.allowed_models:
                return None, types.ErrorData(
                    code=types.INVALID_REQUEST,
                    message=f"sampling model '{model}' is not in the allowed list",
                )
        elif requested and requested != self.default_model:
            # No whitelist → only the resolved default is permitted.
            return None, types.ErrorData(
                code=types.INVALID_REQUEST,
                message=f"sampling model override '{requested}' is not permitted",
            )
        return model, None

    async def run(self, params: Any) -> Any:
        """Execute one sampling/createMessage request under governance.

        Returns CreateMessageResult, CreateMessageResultWithTools, or ErrorData.
        """
        import mcp.types as types

        gov = self.governance

        if not self._limiter.allow():
            return types.ErrorData(
                code=types.INVALID_REQUEST,
                message="sampling rate limit exceeded; slow down requests",
            )

        model, err = self._resolve_model(params)
        if err is not None:
            return err

        max_tokens = min(
            int(getattr(params, "maxTokens", gov.max_tokens_cap)),
            gov.max_tokens_cap,
        )
        system_prompt = getattr(params, "systemPrompt", None)
        temperature = getattr(params, "temperature", None)
        messages = sampling_messages_to_openai(params.messages, system_prompt)

        server_tools = getattr(params, "tools", None)
        within_round_budget = self._tool_rounds < gov.max_tool_rounds
        emit_tools = bool(gov.allow_tools and server_tools and within_round_budget)
        tools = mcp_tools_to_openai(server_tools) if emit_tools else None

        try:
            response = await self.provider.chat_with_retry(
                messages=messages,
                tools=tools,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature if temperature is not None else 0.7,
            )
        except Exception as e:  # noqa: BLE001
            return types.ErrorData(
                code=types.INTERNAL_ERROR,
                message=f"sampling provider error: {e}",
            )

        if response.finish_reason == "error":
            return types.ErrorData(
                code=types.INTERNAL_ERROR,
                message=f"sampling provider error: {(response.content or 'unknown')[:200]}",
            )

        result = llm_response_to_sampling_result(response, model=model, emit_tools=emit_tools)
        if hasattr(result, "stopReason") and result.stopReason == "toolUse":
            self._tool_rounds += 1
        return result
