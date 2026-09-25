"""Restart helpers: the notice shown after a restart, and how this process
restarts itself."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

RESTART_NOTIFY_CHANNEL_ENV = "DURIN_RESTART_NOTIFY_CHANNEL"
RESTART_NOTIFY_CHAT_ID_ENV = "DURIN_RESTART_NOTIFY_CHAT_ID"
RESTART_NOTIFY_METADATA_ENV = "DURIN_RESTART_NOTIFY_METADATA"
RESTART_STARTED_AT_ENV = "DURIN_RESTART_STARTED_AT"

# How this process restarts itself, when something owns an orderly shutdown
# (the gateway). None: the caller shuts down what it can and re-execs.
_RESTART_HANDLER: Callable[[], None] | None = None

# How long a restart's graceful shutdown gets before this process re-execs
# anyway (seconds). Nothing rescues a stuck SIGTERM the way systemd's SIGKILL
# does; a restart has no such backstop unless it brings its own. close_mcp,
# cron, the dream/embed workers, the drain, or asyncio.run's own teardown of
# leftover tasks and executor threads could each hang. Long enough for a
# normal shutdown to finish, short enough that a stuck one still comes back
# soon.
RESTART_SHUTDOWN_DEADLINE_S = 30.0

# Guards ``reexec`` so only the first caller actually replaces the process:
# the normal restart path and the ``arm_restart_deadline`` watchdog run on
# different threads and can both decide to call it around the same moment.
_reexec_lock = threading.Lock()
_reexeced = False


@dataclass(frozen=True)
class RestartNotice:
    channel: str
    chat_id: str
    started_at_raw: str
    metadata: dict[str, Any] = field(default_factory=dict)


def format_restart_completed_message(started_at_raw: str) -> str:
    """Build restart completion text and include elapsed time when available."""
    elapsed_suffix = ""
    if started_at_raw:
        with suppress(ValueError):
            elapsed_s = max(0.0, time.time() - float(started_at_raw))
            elapsed_suffix = f" in {elapsed_s:.1f}s"
    return f"Restart completed{elapsed_suffix}."


def set_restart_handler(handler: Callable[[], None] | None) -> None:
    """Install how this process restarts itself, or clear it with None.

    The gateway installs one that runs its graceful shutdown (the same one a
    SIGTERM runs) and re-execs afterwards.
    """
    global _RESTART_HANDLER
    _RESTART_HANDLER = handler


def request_restart() -> bool:
    """Hand a restart to the installed handler; False when none is installed."""
    handler = _RESTART_HANDLER
    if handler is None:
        return False
    handler()
    return True


def _flush_logs_before_exit(timeout_s: float = 2.0) -> None:
    """Give the log sink's queue a bounded chance to flush before ``os._exit``,
    which skips it (and any atexit hook) entirely otherwise — the gateway's
    file sink queues writes (``enqueue=True``, ``durin/cli/gateway_logging.py``),
    so the record just written could otherwise never reach disk.

    ``logger.complete()`` is documented safe to call from non-async code (its
    own multiprocessing example does exactly that, unawaited) and its
    enqueued-message wait is synchronous either way; run it on its own thread
    with a bounded join anyway, in case the sink itself is what's stuck.
    """
    done = threading.Event()

    def _complete() -> None:
        with suppress(Exception):
            logger.complete()
        done.set()

    threading.Thread(target=_complete, daemon=True).start()
    done.wait(timeout=timeout_s)


def reexec() -> None:
    """Replace this process with a fresh ``python -m durin`` on the same argv.

    Idempotent: only the first caller execs. The normal restart path and the
    ``arm_restart_deadline`` watchdog can both reach this around the same
    moment, from different threads. The loser blocks forever rather than
    returning: ``execv`` needs a moment to actually swap the process image,
    and a caller that returns could let this process reach its own exit —
    exit 0, most likely — in that window, before the winner's ``execv`` ever
    takes effect. Blocking costs nothing, since the winner's ``execv``
    replaces this thread along with everything else in the process the
    instant it succeeds.
    """
    global _reexeced
    with _reexec_lock:
        # Decide under the lock, block outside it: blocking here while still
        # holding it would leave _reexec_lock held forever, and any later
        # caller in the same process (another restart attempt, a test) would
        # then hang acquiring it too — long after a real winner's execv
        # would already have ended everything, but not in a process where
        # execv is mocked away (tests) or genuinely failed.
        lost = _reexeced
        _reexeced = True
    if lost:
        threading.Event().wait()
        return  # pragma: no cover - unreachable; the winner's execv ends this process first
    try:
        os.execv(sys.executable, [sys.executable, "-m", "durin"] + sys.argv[1:])
    except Exception:
        # execv failing (a bad interpreter path, a resource limit) is rare,
        # but nothing else here will retry it — this can be running on the
        # watchdog's own thread, where just returning ends that thread
        # silently and leaves the process running stale code with no
        # gateway restarted. Exit hard so a supervisor (systemd, launchd)
        # restarts the process instead of it limping on unrestarted.
        logger.exception("reexec: os.execv failed; exiting so the supervisor restarts us")
        _flush_logs_before_exit()
        os._exit(1)


def arm_restart_deadline(deadline_s: float | None = None) -> threading.Timer:
    """Start a daemon watchdog that calls ``reexec`` after ``deadline_s``
    (default ``RESTART_SHUTDOWN_DEADLINE_S``) even if the restart's graceful
    shutdown never finishes.

    Runs on its own OS thread, so it fires even while the asyncio event loop
    itself is blocked — a synchronous hang inside a shutdown step is not
    something any ``asyncio`` timeout could rescue. The caller cancels the
    returned timer when the restart it was guarding turns out not to be
    needed after all (a real signal landing during the shutdown).
    """
    if deadline_s is None:
        deadline_s = RESTART_SHUTDOWN_DEADLINE_S
    timer = threading.Timer(deadline_s, reexec)
    timer.daemon = True
    timer.start()
    return timer


def set_restart_notice_to_env(
    *, channel: str, chat_id: str, metadata: dict[str, Any] | None = None,
) -> None:
    """Write restart notice env values for the next process."""
    os.environ[RESTART_NOTIFY_CHANNEL_ENV] = channel
    os.environ[RESTART_NOTIFY_CHAT_ID_ENV] = chat_id
    os.environ[RESTART_STARTED_AT_ENV] = str(time.time())
    if metadata:
        try:
            os.environ[RESTART_NOTIFY_METADATA_ENV] = json.dumps(metadata, default=str)
        except (TypeError, ValueError):
            os.environ.pop(RESTART_NOTIFY_METADATA_ENV, None)
    else:
        os.environ.pop(RESTART_NOTIFY_METADATA_ENV, None)


def consume_restart_notice_from_env() -> RestartNotice | None:
    """Read and clear restart notice env values once for this process."""
    channel = os.environ.pop(RESTART_NOTIFY_CHANNEL_ENV, "").strip()
    chat_id = os.environ.pop(RESTART_NOTIFY_CHAT_ID_ENV, "").strip()
    started_at_raw = os.environ.pop(RESTART_STARTED_AT_ENV, "").strip()
    metadata_raw = os.environ.pop(RESTART_NOTIFY_METADATA_ENV, "").strip()
    if not (channel and chat_id):
        return None
    metadata: dict[str, Any] = {}
    if metadata_raw:
        try:
            parsed = json.loads(metadata_raw)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            metadata = parsed
    return RestartNotice(
        channel=channel,
        chat_id=chat_id,
        started_at_raw=started_at_raw,
        metadata=metadata,
    )


def should_show_cli_restart_notice(notice: RestartNotice, session_id: str) -> bool:
    """Return True when a restart notice should be shown in this CLI session."""
    if notice.channel != "cli":
        return False
    if ":" in session_id:
        _, cli_chat_id = session_id.split(":", 1)
    else:
        cli_chat_id = session_id
    return not notice.chat_id or notice.chat_id == cli_chat_id
