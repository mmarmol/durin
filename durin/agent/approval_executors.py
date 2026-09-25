"""Per-kind hashing and execution for approval requests.

Each kind registers two functions, and optionally a third:

* a hash of the state the request would change. It is computed when the
  request is filed and again right before it runs; a mismatch means the target
  changed after it was reviewed, so the request is stale.
* an executor that performs the recorded payload. It runs server-side, never
  from arguments the model supplies at approval time.
* ``requires``: what the executor needs from ``ExecDeps``. Given the handles
  at hand it returns why they cannot run the request (naming where it can be
  approved instead), or None. It is checked before a record is approved, so a
  process that lacks a handle refuses the approval instead of moving the
  record to approved and then failing.

Kinds register from their own modules; ``_ensure_loaded`` imports them so a
fresh process (CLI, API) can execute any kind. Every kind module exists, so
failing to import one is an error, never a kind that is simply absent.
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

HashFn = Callable[[Path, dict], str]
ExecuteFn = Callable[[Path, dict, "ExecDeps"], Awaitable[dict]]
RequiresFn = Callable[["ExecDeps"], "str | None"]

_REGISTRY: dict[str, tuple[HashFn, ExecuteFn]] = {}
_REQUIRES: dict[str, RequiresFn] = {}
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


def register(kind: str, *, hash_fn: HashFn, execute_fn: ExecuteFn,
             requires: RequiresFn | None = None) -> None:
    _REGISTRY[kind] = (hash_fn, execute_fn)
    if requires is not None:
        _REQUIRES[kind] = requires
    else:
        _REQUIRES.pop(kind, None)


def _ensure_loaded(kind: str) -> tuple[HashFn, ExecuteFn]:
    if kind not in _REGISTRY:
        for mod in _KIND_MODULES:
            importlib.import_module(mod)
    if kind not in _REGISTRY:
        raise ApprovalExecError(f"no executor registered for kind {kind!r}")
    return _REGISTRY[kind]


def missing_handles(kind: str, deps: ExecDeps) -> str | None:
    """Why *deps* cannot run a request of *kind*, or None when they can (or
    the kind declares no requirement)."""
    _ensure_loaded(kind)
    requires = _REQUIRES.get(kind)
    return requires(deps) if requires is not None else None


def current_hash(workspace: Path, record: dict) -> str:
    hash_fn, _ = _ensure_loaded(record["kind"])
    return hash_fn(Path(workspace), record.get("payload") or {})


async def execute(workspace: Path, record: dict, deps: ExecDeps) -> dict:
    _, execute_fn = _ensure_loaded(record["kind"])
    return await execute_fn(Path(workspace), record.get("payload") or {}, deps)
