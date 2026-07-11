"""Inbound trigger matcher: routes a channel message to a claim-waiting run,
a newly fired loop run, or lets it fall through to a normal agent turn.

``handle_inbound(msg)`` runs a synchronous decision — claim wake first
(thread-keyed), then trigger match against enabled loops' channel triggers,
then a concurrency decision — and resolves that decision fast: the actual
runtime call (``answer``/``fire``) is scheduled via ``asyncio.create_task``
so a slow workflow run never blocks bus dispatch. The return value reflects
the DECISION, not the eventual run outcome.

Determinism: loops are evaluated in ascending ``name`` order; the first loop
whose channel trigger fully matches (structural filters, then optional
semantic condition) wins.

Queueing seam: the queue module (a later task) hasn't landed yet. The
constructor accepts an ``enqueue: Callable[[str, dict], None] | None``
callback. When it is ``None`` and the decision would be "queue" (single
concurrency, an active run already exists), the matcher does NOT consume
the message — nothing would ever drain a queue that doesn't exist — it
logs a warning and lets the message fall through as a normal turn instead.
Wiring the real queue means passing ``enqueue``.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

from loguru import logger

from durin.agent.tools._telemetry import emit_tool_event
from durin.loops import claims, run_log, store
from durin.loops.runtime import LoopBusy, LoopsRuntime
from durin.loops.spec import LoopSpec
from durin.telemetry.logger import (
    bind_telemetry,
    current_telemetry,
    get_session_logger,
    reset_telemetry,
)

_SUMMARY_CONTENT_CHARS = 1000


class TriggerMatcher:
    def __init__(self, workspace, *, runtime: LoopsRuntime,
                 semantic_judge: Callable[[str, str], Awaitable[bool]] | None = None,
                 queue_ttl_s: int = 3600,
                 enqueue: Callable[[str, dict], None] | None = None):
        self._ws = workspace
        self._runtime = runtime
        self._semantic_judge = semantic_judge
        self._queue_ttl_s = queue_ttl_s
        self._enqueue = enqueue
        # Loop names with a fire task scheduled but not yet resolved. The
        # busy decision in `_dispatch_match` is synchronous (reads
        # `run_log.active_runs`) while the actual `runtime.fire()` call
        # happens later inside the `asyncio.create_task`'d `_fire` — two
        # messages arriving back-to-back for the same single-concurrency
        # loop would otherwise both read "not active" and both decide to
        # fire, so the second loses the race inside the task (LoopBusy) and
        # gets dropped. This set closes that window: a name is added
        # synchronously before the task is scheduled and removed in the
        # task's `finally`. Single-threaded asyncio event loop — no lock
        # needed, the add/check are never interleaved with the task start.
        self._pending_fires: set[str] = set()

    async def handle_inbound(self, msg: Any) -> bool:
        """Return True when the message was consumed by a loop (claim wake
        or trigger match) and should NOT be dispatched as a normal agent
        turn. Only ``msg.channel == "email"`` is evaluated in V2."""
        if msg.channel != "email":
            return False

        metadata = msg.metadata or {}
        sender = str(metadata.get("sender_email") or msg.sender_id or "")
        subject = str(metadata.get("subject") or "")
        thread = (metadata.get("email") or {}).get("thread")

        if thread and await self._try_wake(thread, msg):
            return True

        for spec in sorted(store.list_loops(self._ws), key=lambda s: s.name):
            if not spec.enabled:
                continue
            if not await self._trigger_matches(spec, sender, subject, msg):
                continue
            return self._dispatch_match(spec, sender, subject, thread, msg)

        return False

    async def _try_wake(self, thread: str, msg: Any) -> bool:
        claim = claims.lookup(self._ws, thread)
        if not claim:
            return False
        loop_name = claim.get("loop")
        run_id = claim.get("run_id")
        record = run_log.read_run(self._ws, loop_name, run_id) if loop_name and run_id else None
        if record and record.get("status") == "waiting_info":
            self._emit(loop_name, msg.channel, "woke")
            asyncio.create_task(self._answer(loop_name, run_id, msg.content))
            return True
        # Stale claim: the run moved on (or vanished) without releasing it.
        claims.release(self._ws, thread)
        return False

    async def _trigger_matches(self, spec: LoopSpec, sender: str, subject: str, msg: Any) -> bool:
        for trigger in spec.triggers:
            if trigger.source != "channel" or trigger.channel != "email":
                continue
            if not self._structural_match(trigger.filters, sender, subject):
                continue
            if trigger.semantic and not await self._semantic_match(trigger.semantic, sender, subject, msg):
                continue
            return True
        return False

    @staticmethod
    def _structural_match(filters: dict, sender: str, subject: str) -> bool:
        from_needle = filters.get("from_contains")
        if from_needle and from_needle.lower() not in sender.lower():
            return False
        subject_needle = filters.get("subject_contains")
        if subject_needle and subject_needle.lower() not in subject.lower():
            return False
        return True

    async def _semantic_match(self, condition: str, sender: str, subject: str, msg: Any) -> bool:
        if self._semantic_judge is None:
            logger.warning(
                "loops: trigger has a semantic condition but no semantic_judge is "
                "configured; treating as no-match"
            )
            return False
        summary = f"From: {sender}\nSubject: {subject}\n\n{msg.content[:_SUMMARY_CONTENT_CHARS]}"
        try:
            return bool(await self._semantic_judge(condition, summary))
        except Exception:
            logger.warning("loops: semantic_judge raised for condition {!r}; treating as no-match", condition)
            return False

    def _dispatch_match(self, spec: LoopSpec, sender: str, subject: str, thread: str | None, msg: Any) -> bool:
        origin = {"channel": msg.channel, "sender": sender, "chat_id": msg.chat_id,
                  "thread": thread, "subject": subject}
        busy = spec.concurrency != "parallel" and (
            spec.name in self._pending_fires or bool(run_log.active_runs(self._ws, spec.name))
        )
        if not busy:
            self._pending_fires.add(spec.name)
            asyncio.create_task(self._fire(spec.name, msg.channel, msg.content, origin))
            return True
        if self._enqueue is not None:
            self._emit(spec.name, msg.channel, "queued")
            self._enqueue(spec.name, self._queue_event(msg.content, origin))
            return True
        logger.warning(
            "loops: loop '{}' matched but is busy (single-concurrency) and no queue "
            "is wired; passing the message through as a normal turn", spec.name,
        )
        self._emit(spec.name, msg.channel, "passed_busy")
        return False

    def _queue_event(self, content: str, origin: dict) -> dict:
        now = time.time()
        return {"content": content, "origin": origin, "received_at": now, "expires_at": now + self._queue_ttl_s}

    async def _fire(self, name: str, channel: str, content: str, origin: dict) -> None:
        try:
            await self._runtime.fire(name, source="channel", task=content, origin=origin)
            self._emit(name, channel, "fired")
        except LoopBusy:
            # Belt and braces: the pending-fires guard should make this
            # unreachable for the sequential-message race it was built for,
            # but keep the fallback for any other path that can still lose
            # the race against `runtime.fire`'s own active_runs check.
            if self._enqueue is not None:
                self._enqueue(name, self._queue_event(content, origin))
                self._emit(name, channel, "queued")
            else:
                logger.warning(
                    "loops: loop '{}' lost the fire race (now busy) and no queue is "
                    "wired; message dropped", name,
                )
                self._emit(name, channel, "passed_busy")
        except Exception:
            logger.exception("loops: fire('{}') failed", name)
        finally:
            self._pending_fires.discard(name)

    async def _answer(self, loop_name: str, run_id: str, content: str) -> None:
        try:
            await self._runtime.answer(loop_name, run_id, content)
        except Exception:
            logger.exception("loops: answer('{}', '{}') failed", loop_name, run_id)

    def _emit(self, loop_name: str, channel: str, action: str) -> None:
        token = None
        if current_telemetry() is None:
            token = bind_telemetry(get_session_logger(f"loop:{loop_name}"))
        try:
            emit_tool_event("loops.event_matched", {"loop": loop_name, "source_channel": channel, "action": action})
        finally:
            if token is not None:
                reset_telemetry(token)
