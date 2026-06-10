"""execute_code: programmatic tool calling via a sandboxed Python script.

The model writes a Python script that calls a whitelisted subset of durin
tools through a generated ``durin_tools`` stub module; tool results stay in
the script's local variables and only stdout returns to the LLM context.
This is token compression for batch operations (read 30 files, filter,
aggregate, print a summary) — one tool result instead of 30.

Design adapted from hermes-agent's code_execution_tool (MIT, Nous Research
2025), simplified for durin: the agent loop is asyncio-native, so the RPC
server is an ``asyncio.start_unix_server`` on the same loop (no background
thread, no sync→async bridge). Local-only v1; see docs/architecture/loop.md.

Security model: the child gets a curated env (HOME/LANG/TERM/PYTHONUNBUFFERED
+ PYTHONPATH for the stub — NO ambient secrets), the RPC server enforces the
tool allowlist and the call cap server-side, and the socket file is 0600 in
a private tempdir.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from loguru import logger
from pydantic import Field

from durin.agent.tools._telemetry import emit_tool_event
from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.schema import StringSchema, tool_parameters_schema
from durin.config.schema import Base

_IS_WINDOWS = sys.platform == "win32"

# Tool classes exposed inside the sandbox. Deliberately excludes exec
# (a Python script can already use subprocess; the env is scrubbed) and
# anything stateful/side-channel (messaging, memory writes, spawn).
_ALLOWED_TOOLS = (
    "read_file",
    "write_file",
    "edit_file",
    "grep",
    "list_dir",
    "web_search",
    "web_fetch",
    "memory_search",
)

# (name, python signature, args-dict expression) for the generated stub.
_STUB_SPECS: tuple[tuple[str, str, str], ...] = (
    ("read_file", "path, start=0, limit=2000",
     "{'path': path, 'start': start, 'limit': limit}"),
    ("write_file", "path, content",
     "{'path': path, 'content': content}"),
    ("edit_file", "path, old_text, new_text, replace_all=False",
     "{'path': path, 'old_text': old_text, 'new_text': new_text, "
     "'replace_all': replace_all}"),
    ("grep", "pattern, path='.', glob=None, output_mode='files_with_matches', head_limit=None",
     "{k: v for k, v in {'pattern': pattern, 'path': path, 'glob': glob, "
     "'output_mode': output_mode, 'head_limit': head_limit}.items() if v is not None}"),
    ("list_dir", "path, recursive=False, max_entries=200, offset=0",
     "{'path': path, 'recursive': recursive, 'max_entries': max_entries, "
     "'offset': offset}"),
    ("web_search", "query, count=5",
     "{'query': query, 'count': count}"),
    ("web_fetch", "url",
     "{'url': url}"),
    ("memory_search",
     "query, scope='all', level='warm', keywords=None, limit=10, kinds='all'",
     "{k: v for k, v in {'query': query, 'scope': scope, 'level': level, "
     "'keywords': keywords, 'limit': limit, 'kinds': kinds}.items() "
     "if v is not None}"),
)

_STUB_HEADER = '''"""durin_tools — RPC stubs for execute_code scripts (generated)."""
import json
import os
import socket
import threading

_sock = None
_lock = threading.Lock()


def _connect():
    global _sock
    if _sock is None:
        _sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        _sock.connect(os.environ["DURIN_RPC_SOCKET"])
        _sock.settimeout(300)
    return _sock


def _call(tool, args):
    with _lock:
        s = _connect()
        s.sendall((json.dumps({"tool": tool, "args": args}) + "\\n").encode())
        buf = b""
        while not buf.endswith(b"\\n"):
            chunk = s.recv(65536)
            if not chunk:
                raise RuntimeError("durin RPC connection closed")
            buf += chunk
    resp = json.loads(buf)
    if not resp.get("ok"):
        raise RuntimeError(resp.get("error", "tool call failed"))
    return resp["result"]
'''


def _generate_stub() -> str:
    parts = [_STUB_HEADER]
    for name, sig, args_expr in _STUB_SPECS:
        parts.append(
            f"\n\ndef {name}({sig}):\n"
            f"    \"\"\"Call durin's {name} tool; returns its text result.\"\"\"\n"
            f"    return _call({name!r}, {args_expr})\n"
        )
    return "".join(parts)


class CodeExecutionConfig(Base):
    """execute_code tool configuration."""

    enable: bool = True
    timeout_s: int = 300
    max_tool_calls: int = 50
    max_stdout_bytes: int = 50_000
    max_stderr_bytes: int = 10_000


@tool_parameters(
    tool_parameters_schema(
        code=StringSchema(
            "Python script to run. Import tools with "
            "'from durin_tools import read_file, grep, ...'. Only what the "
            "script prints (stdout) is returned."
        ),
        required=["code"],
    )
)
class ExecuteCodeTool(Tool):
    """Run a Python script with RPC access to a subset of durin tools."""

    _scopes = {"core", "subagent"}

    config_key = "code_execution"

    @classmethod
    def config_cls(cls):
        return CodeExecutionConfig

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        if _IS_WINDOWS:
            return False  # v1 is UDS-only
        cfg = getattr(ctx.config, "code_execution", None)
        return bool(cfg is None or cfg.enable)

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        # Build private instances of the allowed tools from the same context
        # the loader gives every tool (workspace restriction etc. included).
        from durin.agent.tools.filesystem import (
            EditFileTool,
            ListDirTool,
            ReadFileTool,
            WriteFileTool,
        )
        from durin.agent.tools.memory_search import MemorySearchTool
        from durin.agent.tools.search import GrepTool
        from durin.agent.tools.web import WebFetchTool, WebSearchTool

        instances: dict[str, Tool] = {}
        for tool_cls in (
            ReadFileTool, WriteFileTool, EditFileTool, ListDirTool,
            GrepTool, WebFetchTool, WebSearchTool, MemorySearchTool,
        ):
            try:
                instance = tool_cls.create(ctx)
                instances[instance.name] = instance
            except Exception as e:  # noqa: BLE001 — one tool failing must not kill execute_code
                logger.debug("execute_code: could not build {}: {}", tool_cls.__name__, e)
        cfg = getattr(ctx.config, "code_execution", None) or CodeExecutionConfig()
        return cls(tools=instances, config=cfg, workspace=ctx.workspace)

    def __init__(
        self,
        tools: dict[str, Tool] | None = None,
        config: CodeExecutionConfig | None = None,
        workspace: str | None = None,
    ):
        self._tools = {
            name: tool for name, tool in (tools or {}).items()
            if name in _ALLOWED_TOOLS
        }
        self._config = config or CodeExecutionConfig()
        self._workspace = workspace

    @property
    def name(self) -> str:
        return "execute_code"

    @property
    def description(self) -> str:
        return (
            "Run a Python script that can call durin tools "
            f"({', '.join(sorted(self._tools) or _ALLOWED_TOOLS)}) via "
            "'from durin_tools import ...'. Tool results stay in script "
            "variables; ONLY stdout returns to you — print exactly what you "
            "need. Use for batch operations (read/filter/aggregate many "
            "files or pages) instead of many individual tool calls. "
            f"Limits: {self._config.timeout_s}s, "
            f"{self._config.max_tool_calls} tool calls, 50KB stdout."
        )

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(self, code: str | None = None, **kwargs: Any) -> str:
        if not code:
            return json.dumps({"status": "error", "error": "Unknown code"})
        started = time.monotonic()
        # Per-tool call counts: the primary "how is execute_code actually
        # used" signal — distinguishes a real batch (many calls across a few
        # tools) from a trivial 0-1 call script that a direct tool call would
        # have served better.
        tool_calls: dict[str, int] = {}

        async def handle_client(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
        ) -> None:
            try:
                while True:
                    line = await reader.readline()
                    if not line:
                        break
                    response = await self._dispatch(line, tool_calls)
                    writer.write(response + b"\n")
                    await writer.drain()
            except (ConnectionResetError, asyncio.IncompleteReadError):
                pass
            finally:
                with_suppress_close(writer)

        with tempfile.TemporaryDirectory(prefix="durin_exec_code_") as tmpdir:
            tmp = Path(tmpdir)
            (tmp / "durin_tools.py").write_text(_generate_stub(), encoding="utf-8")
            (tmp / "script.py").write_text(code, encoding="utf-8")
            sock_path = tmp / "rpc.sock"

            server = await asyncio.start_unix_server(
                handle_client, path=str(sock_path),
            )
            os.chmod(sock_path, 0o600)
            try:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, str(tmp / "script.py"),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    stdin=asyncio.subprocess.DEVNULL,
                    cwd=self._workspace or str(tmp),
                    env=self._child_env(tmp),
                )
                status = "success"
                try:
                    stdout, stderr = await asyncio.wait_for(
                        proc.communicate(), timeout=self._config.timeout_s,
                    )
                except asyncio.TimeoutError:
                    status = "timeout"
                    await self._kill_tree(proc)
                    stdout, stderr = b"", b""
            finally:
                server.close()
                await server.wait_closed()

        duration = round(time.monotonic() - started, 3)
        total_calls = sum(tool_calls.values())
        out_text = self._truncate_stdout(stdout.decode("utf-8", errors="replace"))
        err_text = stderr.decode("utf-8", errors="replace")[: self._config.max_stderr_bytes]

        result: dict[str, Any] = {
            "status": status,
            "output": out_text,
            "tool_calls_made": total_calls,
            "duration_seconds": duration,
        }
        if status == "timeout":
            result["error"] = (
                f"Script timed out after {self._config.timeout_s}s and was killed."
            )
        elif proc.returncode != 0:
            result["status"] = "error"
            result["error"] = err_text or f"Script exited with code {proc.returncode}"
            if err_text:
                result["output"] = f"{out_text}\n--- stderr ---\n{err_text}".strip()
        emit_tool_event("tool.execute_code", {
            "status": result["status"],
            "tool_calls_made": total_calls,
            "tool_calls": dict(sorted(tool_calls.items())),
            "code_chars": len(code),
            "duration_ms": round(duration * 1000, 1),
            "stdout_chars": len(out_text),
            "exit_code": proc.returncode,
        })
        return json.dumps(result, ensure_ascii=False)

    async def _dispatch(self, line: bytes, tool_calls: dict[str, int]) -> bytes:
        """Validate + execute one RPC request; never raises."""
        try:
            request = json.loads(line)
            tool_name = request.get("tool", "")
            args = request.get("args") or {}
        except (ValueError, AttributeError):
            return json.dumps({"ok": False, "error": "malformed RPC request"}).encode()

        if tool_name not in self._tools:
            return json.dumps({
                "ok": False,
                "error": f"tool '{tool_name}' is not available in execute_code "
                         f"(allowed: {', '.join(sorted(self._tools))})",
            }).encode()
        if sum(tool_calls.values()) >= self._config.max_tool_calls:
            return json.dumps({
                "ok": False,
                "error": f"Tool call limit reached ({self._config.max_tool_calls}). "
                         "No more tool calls allowed in this execution.",
            }).encode()
        tool_calls[tool_name] = tool_calls.get(tool_name, 0) + 1
        try:
            result = await self._tools[tool_name].execute(**args)
        except Exception as e:  # noqa: BLE001 — tool errors flow back to the script
            return json.dumps({"ok": False, "error": str(e)[:500]}).encode()
        if not isinstance(result, str):
            result = "(non-text tool result omitted in execute_code)"
        return json.dumps({"ok": True, "result": result}, ensure_ascii=False).encode()

    def _child_env(self, tmpdir: Path) -> dict[str, str]:
        """Curated child env — same philosophy as ExecTool._build_env:
        no ambient secrets, just enough to run Python + the RPC stub."""
        return {
            "HOME": os.environ.get("HOME", "/tmp"),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "TERM": os.environ.get("TERM", "dumb"),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(tmpdir),
            "DURIN_RPC_SOCKET": str(tmpdir / "rpc.sock"),
        }

    def _truncate_stdout(self, text: str) -> str:
        """Head 40% / tail 60% with an omission notice (final prints matter)."""
        cap = self._config.max_stdout_bytes
        if len(text) <= cap:
            return text
        head = text[: int(cap * 0.4)]
        tail = text[-int(cap * 0.6):]
        omitted = len(text) - len(head) - len(tail)
        return f"{head}\n\n... [OUTPUT TRUNCATED - {omitted:,} chars omitted] ...\n\n{tail}"

    @staticmethod
    async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
        proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("execute_code child did not die after kill")


def with_suppress_close(writer: asyncio.StreamWriter) -> None:
    """Close a stream writer, swallowing teardown races."""
    try:
        writer.close()
    except Exception:  # noqa: BLE001
        pass
