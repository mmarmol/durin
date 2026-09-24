"""Per-kind hashing and execution for approval requests.

Each kind registers two functions:

* a hash of the state the request would change. It is computed when the
  request is filed and again right before it runs; a mismatch means the target
  changed after it was reviewed, so the request is stale.
* an executor that performs the recorded payload. It runs server-side, never
  from arguments the model supplies at approval time.

Kinds register from their own modules; ``_ensure_loaded`` imports them so a
fresh process (CLI, API) can execute any kind.
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

HashFn = Callable[[Path, dict], str]
ExecuteFn = Callable[[Path, dict, "ExecDeps"], Awaitable[dict]]

_REGISTRY: dict[str, tuple[HashFn, ExecuteFn]] = {}
_KIND_MODULES: tuple[str, ...] = (
    "durin.agent.approval_kinds_skills",
    "durin.agent.approval_kinds_mcp",
    "durin.agent.approval_kinds_exec",
)


@dataclass(frozen=True)
class Prepared:
    kind: str
    summary: str
    detail: dict
    payload: dict
    change_hash: str


@dataclass
class ExecDeps:
    """Runtime handles an executor may need; absent ones make that kind fail
    with a clear error instead of guessing."""
    exec_run: Callable[..., Awaitable[Any]] | None = None
    mcp: Any = None
    attribution: Any = None
    extra: dict = field(default_factory=dict)


class ApprovalExecError(Exception):
    """An executor could not perform the recorded request."""


def register(kind: str, *, hash_fn: HashFn, execute_fn: ExecuteFn) -> None:
    _REGISTRY[kind] = (hash_fn, execute_fn)


def _ensure_loaded(kind: str) -> tuple[HashFn, ExecuteFn]:
    if kind not in _REGISTRY:
        for mod in _KIND_MODULES:
            try:
                importlib.import_module(mod)
            except ModuleNotFoundError:
                continue
    if kind not in _REGISTRY:
        raise ApprovalExecError(f"no executor registered for kind {kind!r}")
    return _REGISTRY[kind]


def current_hash(workspace: Path, record: dict) -> str:
    hash_fn, _ = _ensure_loaded(record["kind"])
    return hash_fn(Path(workspace), record.get("payload") or {})


async def execute(workspace: Path, record: dict, deps: ExecDeps) -> dict:
    _, execute_fn = _ensure_loaded(record["kind"])
    return await execute_fn(Path(workspace), record.get("payload") or {}, deps)
