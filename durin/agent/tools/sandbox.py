"""Sandbox backends for shell command execution.

To add a new backend, implement a function with the signature:
    _wrap_<name>(command: str, workspace: str, cwd: str) -> str
and register it in _BACKENDS below.
"""

from __future__ import annotations

import shlex
from pathlib import Path

from durin.config.paths import get_media_dir


# --- Docker sandbox registry ---
# Maps workspace path (resolved) → container_id.
# The eval harness registers a container before running the agent;
# the _docker backend looks up the container by workspace.
_DOCKER_CONTAINERS: dict[str, str] = {}


def register_docker_container(workspace: str, container_id: str) -> None:
    """Register a Docker container for a workspace path."""
    key = str(Path(workspace).resolve())
    _DOCKER_CONTAINERS[key] = container_id


def unregister_docker_container(workspace: str) -> None:
    """Remove Docker container registration for a workspace."""
    key = str(Path(workspace).resolve())
    _DOCKER_CONTAINERS.pop(key, None)


def _docker(command: str, workspace: str, cwd: str) -> str:
    """Wrap command to run inside a registered Docker container.

    The container must have the workspace bind-mounted at /testbed.
    Commands run with conda env activated.
    """
    key = str(Path(workspace).resolve())
    container_id = _DOCKER_CONTAINERS.get(key)
    if not container_id:
        raise RuntimeError(
            f"No Docker container registered for workspace {workspace!r}. "
            "Call register_docker_container() first."
        )

    wrapped = (
        "source /opt/miniconda3/bin/activate && "
        "conda activate testbed && "
        f"cd /testbed && {command}"
    )
    args = [
        "docker", "exec", container_id,
        "bash", "-c", wrapped,
    ]
    return shlex.join(args)


def _bwrap(command: str, workspace: str, cwd: str) -> str:
    """Wrap command in a bubblewrap sandbox (requires bwrap in container).

    Only the workspace is bind-mounted read-write; its parent dir (which holds
    config.json) is hidden behind a fresh tmpfs.  The media directory is
    bind-mounted read-only so exec commands can read uploaded attachments.
    """
    ws = Path(workspace).resolve()
    media = get_media_dir().resolve()

    try:
        sandbox_cwd = str(ws / Path(cwd).resolve().relative_to(ws))
    except ValueError:
        sandbox_cwd = str(ws)

    required  = ["/usr"]
    optional  = ["/bin", "/lib", "/lib64", "/etc/alternatives",
                 "/etc/ssl/certs", "/etc/resolv.conf", "/etc/ld.so.cache"]

    args = ["bwrap", "--new-session", "--die-with-parent"]
    for p in required: args += ["--ro-bind",     p, p]
    for p in optional: args += ["--ro-bind-try", p, p]
    args += [
        "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
        "--tmpfs", str(ws.parent),        # mask config dir
        "--dir", str(ws),                 # recreate workspace mount point
        "--bind", str(ws), str(ws),
        "--ro-bind-try", str(media), str(media),  # read-only access to media
        "--chdir", sandbox_cwd,
        "--", "sh", "-c", command,
    ]
    return shlex.join(args)


# --- Testbed sandbox (for running inside SWE-bench containers) ---
_TESTBED_PREFIX: str = ""


def register_testbed_prefix(prefix: str) -> None:
    """Set the shell prefix for the testbed sandbox (conda activate + cd)."""
    global _TESTBED_PREFIX
    _TESTBED_PREFIX = prefix


def _testbed(command: str, workspace: str, cwd: str) -> str:
    """Wrap command to run in the testbed conda env.

    Used when the agent runs INSIDE a SWE-bench container.
    The agent process lives in a separate env; exec commands
    need the testbed env for running tests.
    """
    prefix = _TESTBED_PREFIX or (
        "source /opt/miniconda3/bin/activate && "
        "conda activate testbed && "
        "cd /testbed && "
    )
    return f"bash -c {shlex.quote(prefix + command)}"


_BACKENDS = {"bwrap": _bwrap, "docker": _docker, "testbed": _testbed}


def wrap_command(sandbox: str, command: str, workspace: str, cwd: str) -> str:
    """Wrap *command* using the named sandbox backend."""
    if backend := _BACKENDS.get(sandbox):
        return backend(command, workspace, cwd)
    raise ValueError(f"Unknown sandbox backend {sandbox!r}. Available: {list(_BACKENDS)}")
