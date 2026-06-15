"""MCP client: connects to MCP servers and wraps their tools as native durin tools."""

import asyncio
import base64
import json
import os
import re
import shutil
import urllib.parse
from typing import Any

from loguru import logger

from durin.agent.tools.base import Tool
from durin.agent.tools.registry import ToolRegistry
from durin.utils.helpers import build_image_content_blocks

# Transient connection errors that warrant a single retry.
# These typically happen when an MCP server restarts or a network
# connection is interrupted between calls.
_TRANSIENT_EXC_NAMES: frozenset[str] = frozenset((
    "ClosedResourceError",
    "BrokenResourceError",
    "EndOfStream",
    "BrokenPipeError",
    "ConnectionResetError",
    "ConnectionRefusedError",
    "ConnectionAbortedError",
    "ConnectionError",
))

_WINDOWS_SHELL_LAUNCHERS: frozenset[str] = frozenset(("npx", "npm", "pnpm", "yarn", "bunx"))

# Characters allowed in tool names by model providers (Anthropic, OpenAI, etc.).
# Replace anything outside [a-zA-Z0-9_-] with underscore and collapse runs.
_SANITIZE_RE = re.compile(r"_+")


def _sanitize_name(name: str) -> str:
    """Sanitize an MCP-derived name for model API compatibility."""
    return _SANITIZE_RE.sub("_", re.sub(r"[^a-zA-Z0-9_-]", "_", name))


def _disable_output_schema_validation(session: Any) -> None:
    """Disable the SDK's strict outputSchema validation for this session.

    The mcp SDK validates ``structuredContent`` against each tool's
    ``outputSchema`` inside ``call_tool`` (via jsonschema) and RAISES on a
    structurally-invalid schema, an unresolvable ``$ref``, or output that does
    not match the server's own declared schema — turning a successful server
    call into a client-side exception. durin passes ``structuredContent`` to the
    model as a JSON appendix and never depends on it conforming, so we null out
    the SDK's private per-tool schema cache after discovery. Detecting only the
    "broken" schemas is not robust (an unresolvable ``$ref`` only fails when an
    instance exercises it), so we disable validation wholesale. The private
    attribute name is pinned by a test so a SDK rename surfaces in CI.
    """
    cache = getattr(session, "_tool_output_schemas", None)
    if not isinstance(cache, dict):
        return
    for name in list(cache):
        cache[name] = None


def _is_transient(exc: BaseException) -> bool:
    """Check if an exception looks like a transient connection error."""
    return type(exc).__name__ in _TRANSIENT_EXC_NAMES


async def _probe_http_url(url: str, timeout: float = 3.0) -> bool:
    """Quick TCP probe to check if an HTTP MCP server is reachable.

    Avoids entering ``streamable_http_client`` / ``sse_client`` when the port is
    closed — those transports use anyio task groups whose cleanup can raise
    ``RuntimeError`` / ``ExceptionGroup`` that escape the caller's try/except
    and crash the event loop.
    """
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port
    if not port:
        port = 443 if parsed.scheme == "https" else 80
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout,
        )
        writer.close()
        await writer.wait_closed()
        return True
    except (OSError, asyncio.TimeoutError):
        return False


def _windows_command_basename(command: str) -> str:
    """Return the lowercase basename for a Windows command or path."""
    return command.replace("\\", "/").rsplit("/", maxsplit=1)[-1].lower()


def _normalize_windows_stdio_command(
    command: str,
    args: list[str] | None,
    env: dict[str, str] | None,
) -> tuple[str, list[str], dict[str, str] | None]:
    """Wrap Windows shell launchers so MCP stdio servers start reliably."""
    normalized_args = list(args or [])
    if os.name != "nt":
        return command, normalized_args, env

    basename = _windows_command_basename(command)
    if basename in {"cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe"}:
        return command, normalized_args, env

    if basename.endswith((".exe", ".com")):
        return command, normalized_args, env

    resolved = shutil.which(command, path=(env or {}).get("PATH")) or command
    resolved_basename = _windows_command_basename(resolved)
    should_wrap = (
        basename in _WINDOWS_SHELL_LAUNCHERS
        or basename.endswith((".cmd", ".bat"))
        or resolved_basename.endswith((".cmd", ".bat"))
    )
    if not should_wrap:
        return command, normalized_args, env

    comspec = (env or {}).get("COMSPEC") or os.environ.get("COMSPEC") or "cmd.exe"
    return comspec, ["/d", "/c", command, *normalized_args], env


def _extract_nullable_branch(options: Any) -> tuple[dict[str, Any], bool] | None:
    """Return the single non-null branch for nullable unions."""
    if not isinstance(options, list):
        return None

    non_null: list[dict[str, Any]] = []
    saw_null = False
    for option in options:
        if not isinstance(option, dict):
            return None
        if option.get("type") == "null":
            saw_null = True
            continue
        non_null.append(option)

    if saw_null and len(non_null) == 1:
        return non_null[0], True
    return None


def _rewrite_defs(schema: Any) -> Any:
    """Rewrite JSON-Schema `definitions`->`$defs` and matching `$ref`s (Kimi/Moonshot)."""
    if isinstance(schema, dict):
        out: dict[str, Any] = {}
        for key, value in schema.items():
            new_key = "$defs" if key == "definitions" else key
            if key == "$ref" and isinstance(value, str):
                value = value.replace("#/definitions/", "#/$defs/")
            out[new_key] = _rewrite_defs(value)
        return out
    if isinstance(schema, list):
        return [_rewrite_defs(item) for item in schema]
    return schema


def _normalize_schema_for_openai(schema: Any) -> dict[str, Any]:
    """Normalize only nullable JSON Schema patterns for tool definitions."""
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}

    schema = _rewrite_defs(schema)
    normalized = dict(schema)

    raw_type = normalized.get("type")
    if isinstance(raw_type, list):
        non_null = [item for item in raw_type if item != "null"]
        if "null" in raw_type and len(non_null) == 1:
            normalized["type"] = non_null[0]
            normalized["nullable"] = True

    for key in ("oneOf", "anyOf"):
        nullable_branch = _extract_nullable_branch(normalized.get(key))
        if nullable_branch is not None:
            branch, _ = nullable_branch
            merged = {k: v for k, v in normalized.items() if k != key}
            merged.update(branch)
            normalized = merged
            normalized["nullable"] = True
            break

    if "properties" in normalized and isinstance(normalized["properties"], dict):
        normalized["properties"] = {
            name: _normalize_schema_for_openai(prop) if isinstance(prop, dict) else prop
            for name, prop in normalized["properties"].items()
        }

    if "items" in normalized and isinstance(normalized["items"], dict):
        normalized["items"] = _normalize_schema_for_openai(normalized["items"])

    if normalized.get("type") != "object":
        return normalized

    normalized.setdefault("properties", {})
    required = normalized.get("required")
    if isinstance(required, list):
        props = normalized["properties"]
        normalized["required"] = [k for k in required if k in props]
    else:
        normalized.setdefault("required", [])
    return normalized


def _b64_byte_len(data: Any) -> int:
    """Decoded byte length of a base64 string, for size placeholders."""
    try:
        return len(base64.b64decode(data))
    except Exception:
        return 0


def _image_block_or_none(data: Any, mime: Any, label: str) -> list[dict[str, Any]] | None:
    """Native image content blocks, or None when data/mime are missing/invalid.

    Guard for anthropics/issue#90710: emitting an image block with undefined
    data or media-type makes Anthropic 400 and poisons the whole message history
    on every replay. Never emit a half-built image block.
    """
    if not data or not isinstance(mime, str) or not mime.startswith("image/"):
        return None
    try:
        raw = base64.b64decode(data)
    except Exception:
        return None
    if not raw:
        return None
    return build_image_content_blocks(raw, mime, "", label)


def _safe_json(value: Any) -> str:
    """Faithful JSON of an unknown block (forward-compat), never str(block) garbage."""
    dump = getattr(value, "model_dump", None)
    try:
        obj = dump(mode="json") if callable(dump) else value
        return json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        return str(value)


def _embedded_resource_to_parts(resource: Any, types: Any) -> list[Any]:
    """Render an EmbeddedResource's inner contents faithfully."""
    if isinstance(resource, types.TextResourceContents):
        return [resource.text]
    if isinstance(resource, types.BlobResourceContents):
        mime = getattr(resource, "mimeType", None)
        uri = getattr(resource, "uri", "")
        img = _image_block_or_none(resource.blob, mime, f"(MCP resource: {uri})")
        if img is not None:
            return img
        return [f"[MCP embedded resource: {uri}, {_b64_byte_len(resource.blob)} bytes]"]
    return [_safe_json(resource)]


def _content_block_to_parts(block: Any, types: Any) -> list[Any]:
    """Map one MCP ContentBlock to durin result parts (text str or image dict)."""
    if isinstance(block, types.TextContent):
        return [block.text]
    if isinstance(block, types.ImageContent):
        img = _image_block_or_none(block.data, block.mimeType, "(MCP image)")
        return img if img is not None else ["[MCP image: missing or invalid data]"]
    if isinstance(block, types.AudioContent):
        mime = block.mimeType or "unknown"
        return [f"[MCP audio: {mime}, {_b64_byte_len(block.data)} bytes]"]
    if isinstance(block, types.EmbeddedResource):
        return _embedded_resource_to_parts(block.resource, types)
    if isinstance(block, types.ResourceLink):
        label = block.name or block.uri
        return [f"[{label}]({block.uri})"]
    return [_safe_json(block)]


def _parts_to_result(parts: list[Any]) -> str | list[dict[str, Any]]:
    """Join text parts to a string; if any image dict is present, return blocks."""
    if any(isinstance(p, dict) for p in parts):
        return [p if isinstance(p, dict) else {"type": "text", "text": str(p)} for p in parts]
    text = "\n".join(str(p) for p in parts)
    return text or "(no output)"


def _render_tool_result(result: Any, types: Any) -> str | list[dict[str, Any]]:
    """Render a CallToolResult into a faithful string or list of content blocks."""
    parts: list[Any] = []
    for block in result.content:
        parts.extend(_content_block_to_parts(block, types))
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        parts.append("[structuredContent]\n" + _safe_json(structured))
    return _parts_to_result(parts)


def _render_tool_error(result: Any, types: Any) -> str:
    """Render an isError result as an explicit error string (credential redaction is SP-5)."""
    parts = [b.text for b in result.content if isinstance(b, types.TextContent)]
    return "(MCP tool error) " + ("\n".join(parts) or "(unknown error)")


class MCPToolWrapper(Tool):
    """Wraps a single MCP server tool as a durin Tool."""

    _plugin_discoverable = False

    def __init__(self, connection, server_name: str, tool_def, tool_timeout: int = 30):
        self._conn = connection
        self._original_name = tool_def.name
        self._name = _sanitize_name(f"mcp_{server_name}_{tool_def.name}")
        self._description = tool_def.description or tool_def.name
        raw_schema = tool_def.inputSchema or {"type": "object", "properties": {}}
        self._parameters = _normalize_schema_for_openai(raw_schema)
        self._tool_timeout = tool_timeout

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._parameters

    async def execute(self, **kwargs: Any) -> str | list[dict[str, Any]]:
        from mcp import types

        from durin.agent.tools.mcp_connection import _ConnDown

        result = await self._conn.call_tool(
            self._original_name, kwargs, timeout=self._tool_timeout
        )
        if isinstance(result, _ConnDown):
            return result.message
        if getattr(result, "isError", False):
            return _render_tool_error(result, types)
        return _render_tool_result(result, types)


class MCPResourceWrapper(Tool):
    """Wraps an MCP resource URI as a read-only durin Tool."""

    _plugin_discoverable = False

    def __init__(self, connection, server_name: str, resource_def, resource_timeout: int = 30):
        self._conn = connection
        self._uri = resource_def.uri
        self._name = _sanitize_name(f"mcp_{server_name}_resource_{resource_def.name}")
        desc = resource_def.description or resource_def.name
        self._description = f"[MCP Resource] {desc}\nURI: {self._uri}"
        self._parameters: dict[str, Any] = {
            "type": "object",
            "properties": {},
            "required": [],
        }
        self._resource_timeout = resource_timeout

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._parameters

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> str | list[dict[str, Any]]:
        from mcp import types

        from durin.agent.tools.mcp_connection import _ConnDown

        result = await self._conn.read_resource(self._uri, timeout=self._resource_timeout)
        if isinstance(result, _ConnDown):
            return result.message
        parts: list[Any] = []
        for block in result.contents:
            parts.extend(_embedded_resource_to_parts(block, types))
        return _parts_to_result(parts)


class MCPPromptWrapper(Tool):
    """Wraps an MCP prompt as a read-only durin Tool."""

    _plugin_discoverable = False

    def __init__(self, connection, server_name: str, prompt_def, prompt_timeout: int = 30):
        self._conn = connection
        self._prompt_name = prompt_def.name
        self._name = _sanitize_name(f"mcp_{server_name}_prompt_{prompt_def.name}")
        desc = prompt_def.description or prompt_def.name
        self._description = (
            f"[MCP Prompt] {desc}\n"
            "Returns a filled prompt template that can be used as a workflow guide."
        )
        self._prompt_timeout = prompt_timeout

        # Build parameters from prompt arguments
        properties: dict[str, Any] = {}
        required: list[str] = []
        for arg in prompt_def.arguments or []:
            prop: dict[str, Any] = {"type": "string"}
            if getattr(arg, "description", None):
                prop["description"] = arg.description
            properties[arg.name] = prop
            if arg.required:
                required.append(arg.name)
        self._parameters: dict[str, Any] = {
            "type": "object",
            "properties": properties,
            "required": required,
        }

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._parameters

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> str | list[dict[str, Any]]:
        from mcp import types

        from durin.agent.tools.mcp_connection import _ConnDown

        result = await self._conn.get_prompt(self._prompt_name, kwargs, timeout=self._prompt_timeout)
        if isinstance(result, _ConnDown):
            return result.message
        parts: list[Any] = []
        for message in result.messages:
            parts.extend(_content_block_to_parts(message.content, types))
        return _parts_to_result(parts)


async def connect_mcp_servers(
    mcp_servers: dict, registry: ToolRegistry, defer_cb: Any = None
) -> dict[str, Any]:
    """Connect to configured MCP servers, returning one supervised connection each.

    Each server gets its own MCPServerConnection running a dedicated task that
    owns the session lifecycle (connect, discover, serve, reconnect, teardown).
    ``defer_cb`` (optional) is re-invoked after every (re)registration so the
    P3 MCP deferral stays applied across reconnects and list_changed refreshes.
    """
    from durin.agent.tools.mcp_connection import MCPServerConnection

    connections: dict[str, Any] = {}
    for name, cfg in mcp_servers.items():
        conn = MCPServerConnection(name, cfg, registry, defer_cb=defer_cb)
        try:
            ok = await conn.start()
        except Exception as e:  # noqa: BLE001
            hint = _stdio_pollution_hint(e)
            logger.exception("MCP server '{}': failed to connect: {}", name, hint)
            continue
        if ok:
            connections[name] = conn
        else:
            err = conn._error
            hint = _stdio_pollution_hint(err) if err is not None else ""
            logger.exception("MCP server '{}': failed to connect: {}", name, hint)
            await conn.aclose()
    return connections


def _stdio_pollution_hint(exc: BaseException | None) -> str:
    """Return a stdio-pollution hint string if the exception looks like protocol pollution."""
    if exc is None:
        return ""
    text = str(exc).lower()
    if any(
        marker in text
        for marker in ("parse error", "invalid json", "unexpected token", "jsonrpc", "content-length")
    ):
        return (
            " Hint: this looks like stdio protocol pollution. Make sure the MCP server writes "
            "only JSON-RPC to stdout and sends logs/debug output to stderr instead."
        )
    return ""
