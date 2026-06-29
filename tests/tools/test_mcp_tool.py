from __future__ import annotations

import asyncio
import base64 as _b64
import sys
from contextlib import asynccontextmanager
from types import ModuleType, SimpleNamespace

import pytest

import durin.agent.tools.mcp as mcp_mod
from durin.agent.tools.mcp import (
    MCPPromptWrapper,
    MCPResourceWrapper,
    MCPToolWrapper,
    _normalize_windows_stdio_command,
    _sanitize_name,
    connect_mcp_servers,
)
from durin.agent.tools.mcp_connection import MCPServerConnection
from durin.agent.tools.registry import ToolRegistry
from durin.config.schema import MCPServerConfig


class _FakeTextContent:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeTextResourceContents:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeBlobResourceContents:
    def __init__(self, blob: str, uri: str = "file:///x", mime_type: str | None = None) -> None:
        self.blob = blob
        self.uri = uri
        self.mimeType = mime_type


class _FakeImageContent:
    def __init__(self, data: str, mime_type: str | None) -> None:
        self.data = data
        self.mimeType = mime_type


class _FakeAudioContent:
    def __init__(self, data: str, mime_type: str | None) -> None:
        self.data = data
        self.mimeType = mime_type


class _FakeEmbeddedResource:
    def __init__(self, resource: object) -> None:
        self.resource = resource


class _FakeResourceLink:
    def __init__(self, name: str | None, uri: str, description: str | None = None) -> None:
        self.name = name
        self.uri = uri
        self.description = description


class _FakeConn:
    """Stand-in MCPServerConnection for wrapper unit tests.

    Returns whatever its ``session`` yields, mirroring the real
    connection's call surface (call_tool/read_resource/get_prompt).
    Exceptions from the session are caught and returned as _ConnDown,
    matching what the real connection does for non-transient failures.
    """

    def __init__(self, session: object | None) -> None:
        self.session = session
        self.name = "test"

    async def call_tool(self, name, arguments, timeout):
        from durin.agent.tools.mcp_connection import _ConnDown
        if self.session is None:
            return _ConnDown("MCP server 'test' is not connected")
        try:
            return await self.session.call_tool(name, arguments=arguments)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return _ConnDown(f"MCP server 'test' tool call failed: {type(exc).__name__}")

    async def read_resource(self, uri, timeout):
        from durin.agent.tools.mcp_connection import _ConnDown
        if self.session is None:
            return _ConnDown("MCP server 'test' is not connected")
        try:
            return await self.session.read_resource(uri)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return _ConnDown(f"MCP server 'test' resource read failed: {type(exc).__name__}")

    async def get_prompt(self, name, arguments, timeout):
        from durin.agent.tools.mcp_connection import _ConnDown
        if self.session is None:
            return _ConnDown("MCP server 'test' is not connected")
        try:
            return await self.session.get_prompt(name, arguments=arguments)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return _ConnDown(f"MCP server 'test' prompt call failed: {type(exc).__name__}")


@pytest.fixture
def fake_mcp_runtime() -> dict[str, object | None]:
    return {"session": None}


@pytest.fixture(autouse=True)
def _fake_mcp_module(
    monkeypatch: pytest.MonkeyPatch, fake_mcp_runtime: dict[str, object | None]
) -> None:
    mod = ModuleType("mcp")
    mod.types = SimpleNamespace(
        TextContent=_FakeTextContent,
        TextResourceContents=_FakeTextResourceContents,
        BlobResourceContents=_FakeBlobResourceContents,
        ImageContent=_FakeImageContent,
        AudioContent=_FakeAudioContent,
        EmbeddedResource=_FakeEmbeddedResource,
        ResourceLink=_FakeResourceLink,
    )

    class _FakeStdioServerParameters:
        def __init__(self, command: str, args: list[str], env: dict | None = None) -> None:
            self.command = command
            self.args = args
            self.env = env

    class _FakeClientSession:
        def __init__(self, _read: object, _write: object, **_kwargs: object) -> None:
            self._session = fake_mcp_runtime["session"]

        async def __aenter__(self) -> object:
            return self._session

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

    @asynccontextmanager
    async def _fake_stdio_client(_params: object, errlog: object = None):
        yield object(), object()

    @asynccontextmanager
    async def _fake_sse_client(_url: str, httpx_client_factory=None):
        yield object(), object()

    @asynccontextmanager
    async def _fake_streamable_http_client(_url: str, http_client=None):
        yield object(), object(), object()

    mod.ClientSession = _FakeClientSession
    mod.StdioServerParameters = _FakeStdioServerParameters
    monkeypatch.setitem(sys.modules, "mcp", mod)

    client_mod = ModuleType("mcp.client")
    stdio_mod = ModuleType("mcp.client.stdio")
    stdio_mod.stdio_client = _fake_stdio_client
    sse_mod = ModuleType("mcp.client.sse")
    sse_mod.sse_client = _fake_sse_client
    streamable_http_mod = ModuleType("mcp.client.streamable_http")
    streamable_http_mod.streamable_http_client = _fake_streamable_http_client

    monkeypatch.setitem(sys.modules, "mcp.client", client_mod)
    monkeypatch.setitem(sys.modules, "mcp.client.stdio", stdio_mod)
    monkeypatch.setitem(sys.modules, "mcp.client.sse", sse_mod)
    monkeypatch.setitem(sys.modules, "mcp.client.streamable_http", streamable_http_mod)

    shared_mod = ModuleType("mcp.shared")
    exc_mod = ModuleType("mcp.shared.exceptions")

    class _FakeMcpError(Exception):
        def __init__(self, code: int = -1, message: str = "error"):
            self.error = SimpleNamespace(code=code, message=message)
            super().__init__(message)

    exc_mod.McpError = _FakeMcpError
    monkeypatch.setitem(sys.modules, "mcp.shared", shared_mod)
    monkeypatch.setitem(sys.modules, "mcp.shared.exceptions", exc_mod)


def _make_wrapper(session: object, *, timeout: float = 0.1) -> MCPToolWrapper:
    tool_def = SimpleNamespace(
        name="demo",
        description="demo tool",
        inputSchema={"type": "object", "properties": {}},
    )
    return MCPToolWrapper(_FakeConn(session), "test", tool_def, tool_timeout=timeout)


def test_wrapper_preserves_non_nullable_unions() -> None:
    tool_def = SimpleNamespace(
        name="demo",
        description="demo tool",
        inputSchema={
            "type": "object",
            "properties": {
                "value": {
                    "anyOf": [{"type": "string"}, {"type": "integer"}],
                }
            },
        },
    )

    wrapper = MCPToolWrapper(_FakeConn(SimpleNamespace(call_tool=None)), "test", tool_def)

    assert wrapper.parameters["properties"]["value"]["anyOf"] == [
        {"type": "string"},
        {"type": "integer"},
    ]


def test_wrapper_normalizes_nullable_property_type_union() -> None:
    tool_def = SimpleNamespace(
        name="demo",
        description="demo tool",
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": ["string", "null"]},
            },
        },
    )

    wrapper = MCPToolWrapper(_FakeConn(SimpleNamespace(call_tool=None)), "test", tool_def)

    assert wrapper.parameters["properties"]["name"] == {"type": "string", "nullable": True}


def test_wrapper_expands_non_nullable_multi_type_array_to_anyof() -> None:
    tool_def = SimpleNamespace(
        name="demo",
        description="demo tool",
        inputSchema={
            "type": "object",
            "properties": {
                "value": {"type": ["string", "integer"], "description": "x"},
            },
        },
    )

    wrapper = MCPToolWrapper(_FakeConn(SimpleNamespace(call_tool=None)), "test", tool_def)
    prop = wrapper.parameters["properties"]["value"]

    assert prop.get("anyOf") == [{"type": "string"}, {"type": "integer"}]
    assert prop.get("description") == "x"
    assert not isinstance(prop.get("type"), list), "type list must be removed"


def test_wrapper_normalizes_nullable_property_anyof() -> None:
    tool_def = SimpleNamespace(
        name="demo",
        description="demo tool",
        inputSchema={
            "type": "object",
            "properties": {
                "name": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "description": "optional name",
                },
            },
        },
    )

    wrapper = MCPToolWrapper(_FakeConn(SimpleNamespace(call_tool=None)), "test", tool_def)

    assert wrapper.parameters["properties"]["name"] == {
        "type": "string",
        "description": "optional name",
        "nullable": True,
    }


def test_normalize_windows_stdio_command_is_noop_off_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_mod.os, "name", "posix", raising=False)

    command, args, env = _normalize_windows_stdio_command(
        "npx",
        ["-y", "chrome-devtools-mcp@latest"],
        {"FOO": "bar"},
    )

    assert command == "npx"
    assert args == ["-y", "chrome-devtools-mcp@latest"]
    assert env == {"FOO": "bar"}


def test_normalize_windows_stdio_command_wraps_npx_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_mod.os, "name", "nt", raising=False)
    monkeypatch.setattr(
        mcp_mod.shutil,
        "which",
        lambda command, path=None: r"C:\Program Files\nodejs\npx.cmd",
    )
    monkeypatch.setenv("COMSPEC", r"C:\Windows\System32\cmd.exe")

    command, args, env = _normalize_windows_stdio_command(
        "npx",
        ["-y", "chrome-devtools-mcp@latest"],
        None,
    )

    assert command == r"C:\Windows\System32\cmd.exe"
    assert args == ["/d", "/c", "npx", "-y", "chrome-devtools-mcp@latest"]
    assert env is None


def test_normalize_windows_stdio_command_wraps_resolved_cmd_launcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_mod.os, "name", "nt", raising=False)

    def _fake_which(command: str, path: str | None = None) -> str:
        assert command == "custom-launcher"
        assert path == r"C:\Tools"
        return r"C:\Tools\custom-launcher.cmd"

    monkeypatch.setattr(mcp_mod.shutil, "which", _fake_which)
    monkeypatch.setenv("COMSPEC", r"C:\Windows\System32\cmd.exe")

    command, args, _env = _normalize_windows_stdio_command(
        "custom-launcher",
        ["serve"],
        {"PATH": r"C:\Tools"},
    )

    assert command == r"C:\Windows\System32\cmd.exe"
    assert args == ["/d", "/c", "custom-launcher", "serve"]


def test_normalize_windows_stdio_command_keeps_real_executables_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_mod.os, "name", "nt", raising=False)

    command, args, env = _normalize_windows_stdio_command(
        "python.exe",
        ["-m", "http.server"],
        {"FOO": "bar"},
    )

    assert command == "python.exe"
    assert args == ["-m", "http.server"]
    assert env == {"FOO": "bar"}


def test_normalize_windows_stdio_command_skips_existing_shells(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_mod.os, "name", "nt", raising=False)

    command, args, env = _normalize_windows_stdio_command(
        "cmd.exe",
        ["/c", "echo", "hello"],
        None,
    )

    assert command == "cmd.exe"
    assert args == ["/c", "echo", "hello"]
    assert env is None


@pytest.mark.asyncio
async def test_execute_returns_text_blocks() -> None:
    async def call_tool(_name: str, arguments: dict) -> object:
        assert arguments == {"value": 1}
        return SimpleNamespace(content=[_FakeTextContent("hello"), 42])

    wrapper = _make_wrapper(SimpleNamespace(call_tool=call_tool))

    result = await wrapper.execute(value=1)

    assert result == "hello\n42"


# timeout/cancel behavior moved to test_mcp_connection (SP-2 2b/2e)


@pytest.mark.asyncio
async def test_execute_handles_generic_exception() -> None:
    async def call_tool(_name: str, arguments: dict) -> object:
        raise RuntimeError("boom")

    wrapper = _make_wrapper(SimpleNamespace(call_tool=call_tool))

    result = await wrapper.execute()

    assert "failed" in result and "RuntimeError" in result


def _make_tool_def(name: str) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        description=f"{name} tool",
        inputSchema={"type": "object", "properties": {}},
    )


def _make_fake_session(tool_names: list[str]) -> SimpleNamespace:
    async def initialize() -> None:
        return None

    async def list_tools() -> SimpleNamespace:
        return SimpleNamespace(tools=[_make_tool_def(name) for name in tool_names])

    async def list_resources() -> SimpleNamespace:
        return SimpleNamespace(resources=[])

    async def list_prompts() -> SimpleNamespace:
        return SimpleNamespace(prompts=[])

    async def send_ping() -> None:
        return None

    return SimpleNamespace(
        initialize=initialize,
        list_tools=list_tools,
        list_resources=list_resources,
        list_prompts=list_prompts,
        send_ping=send_ping,
    )


@pytest.mark.asyncio
async def test_connect_mcp_servers_enabled_tools_supports_raw_names(
    fake_mcp_runtime: dict[str, object | None],
) -> None:
    fake_mcp_runtime["session"] = _make_fake_session(["demo", "other"])
    registry = ToolRegistry()
    stacks = await connect_mcp_servers(
        {"test": MCPServerConfig(command="fake", enabled_tools=["demo"])},
        registry,
    )
    assert registry.tool_names == ["mcp_test_demo"]
    assert isinstance(list(stacks.values())[0], MCPServerConnection)
    for conn in stacks.values():
        await conn.aclose()


@pytest.mark.asyncio
async def test_connect_mcp_servers_enabled_tools_defaults_to_all(
    fake_mcp_runtime: dict[str, object | None],
) -> None:
    fake_mcp_runtime["session"] = _make_fake_session(["demo", "other"])
    registry = ToolRegistry()
    stacks = await connect_mcp_servers(
        {"test": MCPServerConfig(command="fake")},
        registry,
    )
    assert registry.tool_names == ["mcp_test_demo", "mcp_test_other"]
    for conn in stacks.values():
        await conn.aclose()


@pytest.mark.asyncio
async def test_connect_mcp_servers_enabled_tools_supports_wrapped_names(
    fake_mcp_runtime: dict[str, object | None],
) -> None:
    fake_mcp_runtime["session"] = _make_fake_session(["demo", "other"])
    registry = ToolRegistry()
    stacks = await connect_mcp_servers(
        {"test": MCPServerConfig(command="fake", enabled_tools=["mcp_test_demo"])},
        registry,
    )
    assert registry.tool_names == ["mcp_test_demo"]
    for conn in stacks.values():
        await conn.aclose()


@pytest.mark.asyncio
async def test_connect_mcp_servers_enabled_tools_empty_list_registers_none(
    fake_mcp_runtime: dict[str, object | None],
) -> None:
    fake_mcp_runtime["session"] = _make_fake_session(["demo", "other"])
    registry = ToolRegistry()
    stacks = await connect_mcp_servers(
        {"test": MCPServerConfig(command="fake", enabled_tools=[])},
        registry,
    )
    assert registry.tool_names == []
    for conn in stacks.values():
        await conn.aclose()


@pytest.mark.asyncio
async def test_connect_mcp_servers_enabled_tools_warns_on_unknown_entries(
    fake_mcp_runtime: dict[str, object | None], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_mcp_runtime["session"] = _make_fake_session(["demo"])
    registry = ToolRegistry()
    warnings: list[str] = []

    def _warning(message: str, *args: object) -> None:
        warnings.append(message.format(*args))

    monkeypatch.setattr("durin.agent.tools.mcp_connection.logger.warning", _warning)

    stacks = await connect_mcp_servers(
        {"test": MCPServerConfig(command="fake", enabled_tools=["unknown"])},
        registry,
    )
    assert registry.tool_names == []
    assert warnings
    assert "enabledTools entries not found: unknown" in warnings[-1]
    assert "Available raw names: demo" in warnings[-1]
    assert "Available wrapped names: mcp_test_demo" in warnings[-1]
    for conn in stacks.values():
        await conn.aclose()


@pytest.mark.asyncio
async def test_connect_mcp_servers_logs_stdio_pollution_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages: list[str] = []

    def _error(message: str, *args: object) -> None:
        messages.append(message.format(*args))

    @asynccontextmanager
    async def _broken_stdio_client(_params: object, errlog=None):
        raise RuntimeError("Parse error: Unexpected token 'INFO' before JSON-RPC headers")
        yield  # pragma: no cover

    monkeypatch.setattr(sys.modules["mcp.client.stdio"], "stdio_client", _broken_stdio_client)
    # start() catches the stdio error and returns False (no active exception), so
    # the failure is logged via logger.error — not logger.exception.
    monkeypatch.setattr("durin.agent.tools.mcp.logger.error", _error)

    registry = ToolRegistry()
    stacks = await connect_mcp_servers({"gh": MCPServerConfig(command="github-mcp")}, registry)

    assert stacks == {}
    assert messages
    assert "stdio protocol pollution" in messages[-1]
    assert "stdout" in messages[-1]
    assert "stderr" in messages[-1]


@pytest.mark.asyncio
async def test_connect_mcp_servers_one_failure_does_not_block_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = {"good": _make_fake_session(["demo"])}

    class _SelectiveClientSession:
        def __init__(self, read: object, _write: object, **_kwargs: object) -> None:
            self._session = sessions[read]

        async def __aenter__(self) -> object:
            return self._session

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

    @asynccontextmanager
    async def _selective_stdio_client(params: object, errlog=None):
        if params.command == "bad":
            raise RuntimeError("boom")
        yield params.command, object()

    monkeypatch.setattr(sys.modules["mcp"], "ClientSession", _SelectiveClientSession)
    monkeypatch.setattr(sys.modules["mcp.client.stdio"], "stdio_client", _selective_stdio_client)

    registry = ToolRegistry()
    stacks = await connect_mcp_servers(
        {
            "good": MCPServerConfig(command="good"),
            "bad": MCPServerConfig(command="bad"),
        },
        registry,
    )
    assert registry.tool_names == ["mcp_good_demo"]
    assert set(stacks) == {"good"}
    for conn in stacks.values():
        await conn.aclose()


@pytest.mark.asyncio
async def test_connect_mcp_servers_wraps_windows_stdio_launchers(
    fake_mcp_runtime: dict[str, object | None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_mcp_runtime["session"] = _make_fake_session(["demo"])
    captured: dict[str, object] = {}

    @asynccontextmanager
    async def _capturing_stdio_client(params: object, errlog=None):
        captured["command"] = params.command
        captured["args"] = params.args
        captured["env"] = params.env
        yield object(), object()

    monkeypatch.setattr(mcp_mod.os, "name", "nt", raising=False)
    monkeypatch.setattr(
        mcp_mod.shutil,
        "which",
        lambda command, path=None: r"C:\Program Files\nodejs\npx.cmd",
    )
    monkeypatch.setenv("COMSPEC", r"C:\Windows\System32\cmd.exe")
    monkeypatch.setattr(sys.modules["mcp.client.stdio"], "stdio_client", _capturing_stdio_client)

    registry = ToolRegistry()
    stacks = await connect_mcp_servers(
        {
            "test": MCPServerConfig(
                command="npx",
                args=["-y", "chrome-devtools-mcp@latest"],
            )
        },
        registry,
    )
    for conn in stacks.values():
        await conn.aclose()

    assert captured["command"] == r"C:\Windows\System32\cmd.exe"
    assert captured["args"] == ["/d", "/c", "npx", "-y", "chrome-devtools-mcp@latest"]
    assert captured["env"] is None


# ---------------------------------------------------------------------------
# MCPResourceWrapper tests
# ---------------------------------------------------------------------------


def _make_resource_def(
    name: str = "myres",
    uri: str = "file:///tmp/data.txt",
    description: str = "A test resource",
) -> SimpleNamespace:
    return SimpleNamespace(name=name, uri=uri, description=description)


def _make_resource_wrapper(session: object, *, timeout: float = 0.1) -> MCPResourceWrapper:
    return MCPResourceWrapper(_FakeConn(session), "srv", _make_resource_def(), resource_timeout=timeout)


def test_resource_wrapper_properties() -> None:
    wrapper = MCPResourceWrapper(_FakeConn(None), "myserver", _make_resource_def())
    assert wrapper.name == "mcp_myserver_resource_myres"
    assert "[MCP Resource]" in wrapper.description
    assert "A test resource" in wrapper.description
    assert "file:///tmp/data.txt" in wrapper.description
    assert wrapper.parameters == {"type": "object", "properties": {}, "required": []}
    assert wrapper.read_only is True


@pytest.mark.asyncio
async def test_resource_wrapper_execute_returns_text() -> None:
    async def read_resource(uri: str) -> object:
        assert uri == "file:///tmp/data.txt"
        return SimpleNamespace(
            contents=[_FakeTextResourceContents("line1"), _FakeTextResourceContents("line2")]
        )

    wrapper = _make_resource_wrapper(SimpleNamespace(read_resource=read_resource))
    result = await wrapper.execute()
    assert result == "line1\nline2"


@pytest.mark.asyncio
async def test_resource_wrapper_execute_handles_blob() -> None:
    async def read_resource(uri: str) -> object:
        blob = _b64.b64encode(b"\x00\x01\x02").decode()
        return SimpleNamespace(
            contents=[_FakeBlobResourceContents(blob, uri="file:///x.bin")]
        )

    wrapper = _make_resource_wrapper(SimpleNamespace(read_resource=read_resource))
    result = await wrapper.execute()
    assert "MCP embedded resource" in result
    assert "3 bytes" in result


# timeout behavior moved to test_mcp_connection (SP-2 2b/2e)


@pytest.mark.asyncio
async def test_resource_wrapper_execute_handles_error() -> None:
    async def read_resource(uri: str) -> object:
        raise RuntimeError("boom")

    wrapper = _make_resource_wrapper(SimpleNamespace(read_resource=read_resource))
    result = await wrapper.execute()
    assert "failed" in result and "RuntimeError" in result


# ---------------------------------------------------------------------------
# MCPPromptWrapper tests
# ---------------------------------------------------------------------------


def _make_prompt_def(
    name: str = "myprompt",
    description: str = "A test prompt",
    arguments: list | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(name=name, description=description, arguments=arguments)


def _make_prompt_wrapper(session: object, *, timeout: float = 0.1) -> MCPPromptWrapper:
    return MCPPromptWrapper(_FakeConn(session), "srv", _make_prompt_def(), prompt_timeout=timeout)


def test_prompt_wrapper_properties() -> None:
    arg1 = SimpleNamespace(name="topic", required=True)
    arg2 = SimpleNamespace(name="style", required=False)
    wrapper = MCPPromptWrapper(_FakeConn(None), "myserver", _make_prompt_def(arguments=[arg1, arg2]))
    assert wrapper.name == "mcp_myserver_prompt_myprompt"
    assert "[MCP Prompt]" in wrapper.description
    assert "A test prompt" in wrapper.description
    assert "workflow guide" in wrapper.description
    assert wrapper.parameters["properties"]["topic"] == {"type": "string"}
    assert wrapper.parameters["properties"]["style"] == {"type": "string"}
    assert wrapper.parameters["required"] == ["topic"]
    assert wrapper.read_only is True


def test_prompt_wrapper_no_arguments() -> None:
    wrapper = MCPPromptWrapper(_FakeConn(None), "myserver", _make_prompt_def())
    assert wrapper.parameters == {"type": "object", "properties": {}, "required": []}


def test_prompt_wrapper_preserves_argument_descriptions() -> None:
    arg = SimpleNamespace(name="topic", required=True, description="The subject to discuss")
    wrapper = MCPPromptWrapper(_FakeConn(None), "srv", _make_prompt_def(arguments=[arg]))
    assert wrapper.parameters["properties"]["topic"] == {
        "type": "string",
        "description": "The subject to discuss",
    }


@pytest.mark.asyncio
async def test_prompt_wrapper_execute_returns_text() -> None:
    async def get_prompt(name: str, arguments: dict | None = None) -> object:
        assert name == "myprompt"
        msg1 = SimpleNamespace(
            role="user",
            content=_FakeTextContent("You are an expert on {{topic}}."),
        )
        msg2 = SimpleNamespace(
            role="assistant",
            content=_FakeTextContent("Understood. Ask me anything."),
        )
        return SimpleNamespace(messages=[msg1, msg2])

    wrapper = _make_prompt_wrapper(SimpleNamespace(get_prompt=get_prompt))
    result = await wrapper.execute(topic="AI")
    assert "You are an expert on {{topic}}." in result
    assert "Understood. Ask me anything." in result


# timeout behavior moved to test_mcp_connection (SP-2 2b/2e)


# mcp_error behavior moved to test_mcp_connection (SP-2 2b/2e)


@pytest.mark.asyncio
async def test_prompt_wrapper_execute_handles_error() -> None:
    async def get_prompt(name: str, arguments: dict | None = None) -> object:
        raise RuntimeError("boom")

    wrapper = _make_prompt_wrapper(SimpleNamespace(get_prompt=get_prompt))
    result = await wrapper.execute()
    assert "failed" in result and "RuntimeError" in result


# ---------------------------------------------------------------------------
# connect_mcp_servers: resources + prompts integration
# ---------------------------------------------------------------------------


def _make_fake_session_with_capabilities(
    tool_names: list[str],
    resource_names: list[str] | None = None,
    prompt_names: list[str] | None = None,
) -> SimpleNamespace:
    async def initialize() -> None:
        return None

    async def list_tools() -> SimpleNamespace:
        return SimpleNamespace(tools=[_make_tool_def(name) for name in tool_names])

    async def list_resources() -> SimpleNamespace:
        resources = []
        for rname in resource_names or []:
            resources.append(
                SimpleNamespace(
                    name=rname,
                    uri=f"file:///{rname}",
                    description=f"{rname} resource",
                )
            )
        return SimpleNamespace(resources=resources)

    async def list_prompts() -> SimpleNamespace:
        prompts = []
        for pname in prompt_names or []:
            prompts.append(
                SimpleNamespace(
                    name=pname,
                    description=f"{pname} prompt",
                    arguments=None,
                )
            )
        return SimpleNamespace(prompts=prompts)

    async def send_ping() -> None:
        return None

    return SimpleNamespace(
        initialize=initialize,
        list_tools=list_tools,
        list_resources=list_resources,
        list_prompts=list_prompts,
        send_ping=send_ping,
    )


@pytest.mark.asyncio
async def test_connect_registers_resources_and_prompts(
    fake_mcp_runtime: dict[str, object | None],
) -> None:
    fake_mcp_runtime["session"] = _make_fake_session_with_capabilities(
        tool_names=["tool_a"],
        resource_names=["res_b"],
        prompt_names=["prompt_c"],
    )
    registry = ToolRegistry()
    stacks = await connect_mcp_servers(
        {"test": MCPServerConfig(command="fake")},
        registry,
    )
    assert "mcp_test_tool_a" in registry.tool_names
    assert "mcp_test_resource_res_b" in registry.tool_names
    assert "mcp_test_prompt_prompt_c" in registry.tool_names
    for conn in stacks.values():
        await conn.aclose()


# ---------------------------------------------------------------------------
# _sanitize_name tests
# ---------------------------------------------------------------------------


def test_sanitize_name_replaces_spaces() -> None:
    assert _sanitize_name("PostgreSQL System Information") == "PostgreSQL_System_Information"


def test_sanitize_name_replaces_special_characters() -> None:
    assert _sanitize_name("foo.bar@baz!") == "foo_bar_baz_"


def test_sanitize_name_collapses_consecutive_underscores() -> None:
    assert _sanitize_name("a   b") == "a_b"


def test_sanitize_name_preserves_valid_characters() -> None:
    assert _sanitize_name("my-tool_v2") == "my-tool_v2"


def test_sanitize_name_noop_for_already_clean_names() -> None:
    assert _sanitize_name("mcp_server_tool") == "mcp_server_tool"


# ---------------------------------------------------------------------------
# Wrapper sanitization tests
# ---------------------------------------------------------------------------


def test_tool_wrapper_sanitizes_name() -> None:
    tool_def = SimpleNamespace(
        name="My Tool",
        description="tool with spaces",
        inputSchema={"type": "object", "properties": {}},
    )
    wrapper = MCPToolWrapper(_FakeConn(SimpleNamespace(call_tool=None)), "srv", tool_def)
    assert wrapper.name == "mcp_srv_My_Tool"


def test_resource_wrapper_sanitizes_name() -> None:
    resource_def = SimpleNamespace(
        name="PostgreSQL System Information",
        uri="file:///pg/info",
        description="PG info",
    )
    wrapper = MCPResourceWrapper(_FakeConn(None), "srv", resource_def)
    assert wrapper.name == "mcp_srv_resource_PostgreSQL_System_Information"


def test_prompt_wrapper_sanitizes_name() -> None:
    prompt_def = SimpleNamespace(
        name="design-schema",
        description="Design schema",
        arguments=None,
    )
    # Hyphens are allowed, so this should pass through unchanged
    wrapper = MCPPromptWrapper(_FakeConn(None), "my server", prompt_def)
    assert wrapper.name == "mcp_my_server_prompt_design-schema"


def test_tool_wrapper_preserves_original_name_for_mcp_call() -> None:
    tool_def = SimpleNamespace(
        name="My Tool",
        description="tool with spaces",
        inputSchema={"type": "object", "properties": {}},
    )
    wrapper = MCPToolWrapper(_FakeConn(SimpleNamespace(call_tool=None)), "srv", tool_def)
    # The sanitized API-facing name differs from the original MCP name
    assert wrapper.name == "mcp_srv_My_Tool"
    assert wrapper._original_name == "My Tool"


@pytest.mark.asyncio
async def test_connect_mcp_servers_sanitizes_resource_names(
    fake_mcp_runtime: dict[str, object | None],
) -> None:
    fake_mcp_runtime["session"] = _make_fake_session_with_capabilities(
        tool_names=[],
        resource_names=["PostgreSQL System Information"],
        prompt_names=[],
    )
    registry = ToolRegistry()
    stacks = await connect_mcp_servers(
        {"test": MCPServerConfig(command="fake")},
        registry,
    )
    assert "mcp_test_resource_PostgreSQL_System_Information" in registry.tool_names
    for conn in stacks.values():
        await conn.aclose()


@pytest.mark.asyncio
async def test_connect_mcp_servers_enabled_tools_matches_sanitized_name(
    fake_mcp_runtime: dict[str, object | None],
) -> None:
    fake_mcp_runtime["session"] = _make_fake_session_with_capabilities(
        tool_names=["My Tool", "other"],
    )
    registry = ToolRegistry()
    stacks = await connect_mcp_servers(
        {"test": MCPServerConfig(command="fake", enabled_tools=["mcp_test_My_Tool"])},
        registry,
    )
    assert registry.tool_names == ["mcp_test_My_Tool"]
    for conn in stacks.values():
        await conn.aclose()


# ---------------------------------------------------------------------------
# Result fidelity: ImageContent + guard #90710
# ---------------------------------------------------------------------------

_PNG_1PX = _b64.b64encode(
    bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000a49444154789c6360000002000100ffff03000006000557bfabd400"
        "00000049454e44ae426082"
    )
).decode()


@pytest.mark.asyncio
async def test_execute_image_content_returns_image_block() -> None:
    async def call_tool(_name: str, arguments: dict) -> object:
        return SimpleNamespace(
            content=[_FakeImageContent(_PNG_1PX, "image/png")], isError=False
        )

    wrapper = _make_wrapper(SimpleNamespace(call_tool=call_tool))
    result = await wrapper.execute()

    assert isinstance(result, list)
    image_blocks = [b for b in result if b["type"] == "image_url"]
    assert len(image_blocks) == 1
    assert image_blocks[0]["image_url"]["url"].startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_execute_image_content_missing_data_falls_back_to_text() -> None:
    async def call_tool(_name: str, arguments: dict) -> object:
        return SimpleNamespace(
            content=[_FakeImageContent("", "image/png")], isError=False
        )

    wrapper = _make_wrapper(SimpleNamespace(call_tool=call_tool))
    result = await wrapper.execute()

    assert isinstance(result, str)
    assert "MCP image" in result


@pytest.mark.asyncio
async def test_execute_image_content_non_image_mime_falls_back_to_text() -> None:
    async def call_tool(_name: str, arguments: dict) -> object:
        return SimpleNamespace(
            content=[_FakeImageContent(_PNG_1PX, "application/octet-stream")],
            isError=False,
        )

    wrapper = _make_wrapper(SimpleNamespace(call_tool=call_tool))
    result = await wrapper.execute()

    assert isinstance(result, str)
    assert "MCP image" in result


@pytest.mark.asyncio
async def test_execute_text_only_still_returns_string() -> None:
    async def call_tool(_name: str, arguments: dict) -> object:
        return SimpleNamespace(content=[_FakeTextContent("hello"), 42], isError=False)

    wrapper = _make_wrapper(SimpleNamespace(call_tool=call_tool))
    result = await wrapper.execute()

    assert result == "hello\n42"


@pytest.mark.asyncio
async def test_execute_is_error_returns_error_marker() -> None:
    async def call_tool(_name: str, arguments: dict) -> object:
        return SimpleNamespace(
            content=[_FakeTextContent("boom: bad arg")], isError=True
        )

    wrapper = _make_wrapper(SimpleNamespace(call_tool=call_tool))
    result = await wrapper.execute()

    assert isinstance(result, str)
    assert result.startswith("(MCP tool error)")
    assert "boom: bad arg" in result


@pytest.mark.asyncio
async def test_execute_is_error_empty_content() -> None:
    async def call_tool(_name: str, arguments: dict) -> object:
        return SimpleNamespace(content=[], isError=True)

    wrapper = _make_wrapper(SimpleNamespace(call_tool=call_tool))
    result = await wrapper.execute()

    assert result == "(MCP tool error) (unknown error)"


@pytest.mark.asyncio
async def test_execute_audio_content_placeholder() -> None:
    async def call_tool(_name: str, arguments: dict) -> object:
        data = _b64.b64encode(b"\x00\x01\x02\x03").decode()
        return SimpleNamespace(
            content=[_FakeAudioContent(data, "audio/wav")], isError=False
        )

    wrapper = _make_wrapper(SimpleNamespace(call_tool=call_tool))
    result = await wrapper.execute()
    assert result == "[MCP audio: audio/wav, 4 bytes]"


@pytest.mark.asyncio
async def test_execute_embedded_text_resource() -> None:
    async def call_tool(_name: str, arguments: dict) -> object:
        res = _FakeTextResourceContents("embedded text")
        return SimpleNamespace(content=[_FakeEmbeddedResource(res)], isError=False)

    wrapper = _make_wrapper(SimpleNamespace(call_tool=call_tool))
    result = await wrapper.execute()
    assert result == "embedded text"


@pytest.mark.asyncio
async def test_execute_embedded_blob_image_returns_image_block() -> None:
    async def call_tool(_name: str, arguments: dict) -> object:
        res = _FakeBlobResourceContents(_PNG_1PX, uri="file:///pic.png", mime_type="image/png")
        return SimpleNamespace(content=[_FakeEmbeddedResource(res)], isError=False)

    wrapper = _make_wrapper(SimpleNamespace(call_tool=call_tool))
    result = await wrapper.execute()
    assert isinstance(result, list)
    assert any(b["type"] == "image_url" for b in result)


@pytest.mark.asyncio
async def test_execute_embedded_blob_non_image_placeholder() -> None:
    async def call_tool(_name: str, arguments: dict) -> object:
        blob = _b64.b64encode(b"\x00\x01\x02").decode()
        res = _FakeBlobResourceContents(blob, uri="file:///x.bin", mime_type="application/octet-stream")
        return SimpleNamespace(content=[_FakeEmbeddedResource(res)], isError=False)

    wrapper = _make_wrapper(SimpleNamespace(call_tool=call_tool))
    result = await wrapper.execute()
    assert "MCP embedded resource" in result
    assert "file:///x.bin" in result


@pytest.mark.asyncio
async def test_execute_resource_link() -> None:
    async def call_tool(_name: str, arguments: dict) -> object:
        link = _FakeResourceLink("docs", "https://example.com/docs")
        return SimpleNamespace(content=[link], isError=False)

    wrapper = _make_wrapper(SimpleNamespace(call_tool=call_tool))
    result = await wrapper.execute()
    assert result == "[docs](https://example.com/docs)"


@pytest.mark.asyncio
async def test_execute_unknown_block_renders_json() -> None:
    class _Weird:
        def model_dump(self, mode: str = "python") -> dict:
            return {"kind": "weird", "n": 1}

    async def call_tool(_name: str, arguments: dict) -> object:
        return SimpleNamespace(content=[_Weird()], isError=False)

    wrapper = _make_wrapper(SimpleNamespace(call_tool=call_tool))
    result = await wrapper.execute()
    assert result == '{"kind": "weird", "n": 1}'


@pytest.mark.asyncio
async def test_execute_structured_content_appended() -> None:
    async def call_tool(_name: str, arguments: dict) -> object:
        return SimpleNamespace(
            content=[_FakeTextContent("summary")],
            structuredContent={"count": 3},
            isError=False,
        )

    wrapper = _make_wrapper(SimpleNamespace(call_tool=call_tool))
    result = await wrapper.execute()
    assert "summary" in result
    assert "[structuredContent]" in result
    assert '"count": 3' in result


@pytest.mark.asyncio
async def test_execute_structured_content_only() -> None:
    async def call_tool(_name: str, arguments: dict) -> object:
        return SimpleNamespace(content=[], structuredContent={"k": "v"}, isError=False)

    wrapper = _make_wrapper(SimpleNamespace(call_tool=call_tool))
    result = await wrapper.execute()
    assert "[structuredContent]" in result
    assert '"k": "v"' in result


@pytest.mark.asyncio
async def test_execute_no_structured_content_unchanged() -> None:
    async def call_tool(_name: str, arguments: dict) -> object:
        return SimpleNamespace(
            content=[_FakeTextContent("plain")], structuredContent=None, isError=False
        )

    wrapper = _make_wrapper(SimpleNamespace(call_tool=call_tool))
    result = await wrapper.execute()
    assert result == "plain"


@pytest.mark.asyncio
async def test_execute_image_content_bad_base64_falls_back_to_text() -> None:
    async def call_tool(_name: str, arguments: dict) -> object:
        # "a" is not a valid base64 length -> b64decode raises -> guarded
        return SimpleNamespace(
            content=[_FakeImageContent("a", "image/png")], isError=False
        )

    wrapper = _make_wrapper(SimpleNamespace(call_tool=call_tool))
    result = await wrapper.execute()
    assert isinstance(result, str)
    assert "MCP image" in result


@pytest.mark.asyncio
async def test_execute_image_content_empty_decoded_falls_back_to_text() -> None:
    async def call_tool(_name: str, arguments: dict) -> object:
        # whitespace decodes to empty bytes -> guarded by `if not raw`
        return SimpleNamespace(
            content=[_FakeImageContent("   ", "image/png")], isError=False
        )

    wrapper = _make_wrapper(SimpleNamespace(call_tool=call_tool))
    result = await wrapper.execute()
    assert isinstance(result, str)
    assert "MCP image" in result


# ---------------------------------------------------------------------------
# _normalize_schema_for_openai: required pruning (Task 5)
# ---------------------------------------------------------------------------


def test_schema_prunes_required_not_in_properties() -> None:
    tool_def = SimpleNamespace(
        name="demo",
        description="demo",
        inputSchema={
            "type": "object",
            "properties": {"a": {"type": "string"}},
            "required": ["a", "ghost"],
        },
    )
    wrapper = MCPToolWrapper(_FakeConn(SimpleNamespace(call_tool=None)), "test", tool_def)
    assert wrapper.parameters["required"] == ["a"]


def test_schema_keeps_valid_required() -> None:
    tool_def = SimpleNamespace(
        name="demo",
        description="demo",
        inputSchema={
            "type": "object",
            "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
            "required": ["a", "b"],
        },
    )
    wrapper = MCPToolWrapper(_FakeConn(SimpleNamespace(call_tool=None)), "test", tool_def)
    assert wrapper.parameters["required"] == ["a", "b"]


# ---------------------------------------------------------------------------
# _normalize_schema_for_openai: $defs rewrite (Task 6)
# ---------------------------------------------------------------------------


def test_schema_rewrites_definitions_to_defs() -> None:
    tool_def = SimpleNamespace(
        name="demo",
        description="demo",
        inputSchema={
            "type": "object",
            "properties": {"x": {"$ref": "#/definitions/Foo"}},
            "definitions": {"Foo": {"type": "string"}},
        },
    )
    wrapper = MCPToolWrapper(_FakeConn(SimpleNamespace(call_tool=None)), "test", tool_def)
    params = wrapper.parameters
    assert "definitions" not in params
    assert params["$defs"] == {"Foo": {"type": "string"}}
    assert params["properties"]["x"]["$ref"] == "#/$defs/Foo"


def test_schema_rewrites_nested_defs_ref_in_anyof() -> None:
    tool_def = SimpleNamespace(
        name="demo",
        description="demo",
        inputSchema={
            "type": "object",
            "properties": {
                "x": {"anyOf": [{"$ref": "#/definitions/Foo"}, {"$ref": "#/definitions/Bar"}]},
            },
            "definitions": {"Foo": {"type": "string"}, "Bar": {"type": "integer"}},
        },
    )
    wrapper = MCPToolWrapper(_FakeConn(SimpleNamespace(call_tool=None)), "test", tool_def)
    params = wrapper.parameters
    assert "definitions" not in params
    assert set(params["$defs"]) == {"Foo", "Bar"}
    refs = [b["$ref"] for b in params["properties"]["x"]["anyOf"]]
    assert refs == ["#/$defs/Foo", "#/$defs/Bar"]


# ---------------------------------------------------------------------------
# Output-schema validation opt-out: the SDK validates structuredContent against
# outputSchema in call_tool and raises (broken schema / unresolvable $ref /
# non-conforming output). durin passes structuredContent to the model as a JSON
# appendix and does not need this, so we disable it by nulling the SDK's cache.
# ---------------------------------------------------------------------------


def test_disable_output_schema_validation_nulls_all() -> None:
    from durin.agent.tools.mcp import _disable_output_schema_validation

    session = SimpleNamespace(
        _tool_output_schemas={
            "good": {"type": "object", "properties": {"n": {"type": "integer"}}},
            "broken": {"type": "object", "properties": {"x": {"$ref": "#/$defs/Missing"}}},
        }
    )
    _disable_output_schema_validation(session)
    assert session._tool_output_schemas == {"good": None, "broken": None}


def test_disable_output_schema_validation_no_attr_is_noop() -> None:
    from durin.agent.tools.mcp import _disable_output_schema_validation

    session = SimpleNamespace()  # no _tool_output_schemas
    _disable_output_schema_validation(session)  # must not raise


@pytest.mark.asyncio
async def test_connect_disables_output_schema_validation(
    fake_mcp_runtime: dict[str, object | None],
) -> None:
    session = _make_fake_session(["demo"])
    session._tool_output_schemas = {
        "demo": {"type": "object", "properties": {"x": {"$ref": "#/$defs/Missing"}}}
    }
    fake_mcp_runtime["session"] = session
    registry = ToolRegistry()
    stacks = await connect_mcp_servers(
        {"test": MCPServerConfig(command="fake")}, registry
    )
    assert session._tool_output_schemas["demo"] is None
    for conn in stacks.values():
        await conn.aclose()


@pytest.mark.asyncio
async def test_resource_wrapper_execute_image_blob_returns_image_block() -> None:
    async def read_resource(uri: str) -> object:
        return SimpleNamespace(
            contents=[_FakeBlobResourceContents(_PNG_1PX, uri="file:///pic.png", mime_type="image/png")]
        )

    wrapper = _make_resource_wrapper(SimpleNamespace(read_resource=read_resource))
    result = await wrapper.execute()
    assert isinstance(result, list)
    assert any(b["type"] == "image_url" for b in result)


@pytest.mark.asyncio
async def test_prompt_wrapper_execute_image_message_returns_image_block() -> None:
    async def get_prompt(name: str, arguments: dict | None = None) -> object:
        msg = SimpleNamespace(role="user", content=_FakeImageContent(_PNG_1PX, "image/png"))
        return SimpleNamespace(messages=[msg])

    wrapper = _make_prompt_wrapper(SimpleNamespace(get_prompt=get_prompt))
    result = await wrapper.execute(topic="x")
    assert isinstance(result, list)
    assert any(b["type"] == "image_url" for b in result)


# ---------------------------------------------------------------------------
# read_only honors the MCP `readOnlyHint` annotation
# ---------------------------------------------------------------------------


def test_mcp_tool_read_only_defaults_false_without_annotation() -> None:
    """No annotation → not concurrency-safe (runs alone). Back-compat: existing
    MCP tools never auto-parallelize."""
    wrapper = _make_wrapper(SimpleNamespace(call_tool=None))
    assert wrapper.read_only is False
    assert wrapper.concurrency_safe is False


def test_mcp_tool_read_only_true_when_hint_set() -> None:
    tool_def = SimpleNamespace(
        name="demo",
        description="demo tool",
        inputSchema={"type": "object", "properties": {}},
        annotations=SimpleNamespace(readOnlyHint=True),
    )
    wrapper = MCPToolWrapper(_FakeConn(SimpleNamespace(call_tool=None)), "test", tool_def)
    assert wrapper.read_only is True
    assert wrapper.concurrency_safe is True


def test_mcp_tool_read_only_false_when_hint_false() -> None:
    tool_def = SimpleNamespace(
        name="demo",
        description="demo tool",
        inputSchema={"type": "object", "properties": {}},
        annotations=SimpleNamespace(readOnlyHint=False),
    )
    wrapper = MCPToolWrapper(_FakeConn(SimpleNamespace(call_tool=None)), "test", tool_def)
    assert wrapper.read_only is False
