"""Inbound trigger matcher: routes a channel message to a claim-waiting run,
a newly fired loop run, or lets it fall through to a normal agent turn.

Channel-agnostic: ``channel_meta.extract(msg)`` turns any supported inbound
message (email/slack/telegram/discord/whatsapp) into channel-neutral
``InboundFacts``; an unsupported channel (cli, websocket, cron, ...) yields
``None`` and the message passes through untouched.

``handle_inbound(msg)`` runs a synchronous decision — claim wake first, then
trigger match against enabled loops' channel triggers, then a concurrency
decision — and resolves that decision fast: the actual runtime call
(``answer``/``fire``) is scheduled via ``asyncio.create_task`` so a slow
workflow run never blocks bus dispatch. The return value reflects the
DECISION, not the eventual run outcome.

Claim-wake key precedence: for each enabled loop's channel trigger matching
this message's channel that declares a ``correlate`` pattern, a custom key
(``custom:<loop>:<captured-group>``) is derived from the message and tried
first; only if none of those wake a claim does the matcher fall back to the
message's plain per-channel ``thread_key``. This lets an operator-defined
correlation id (e.g. a ticket number mentioned anywhere in the text) reunite
messages that land in unrelated threads, while the default thread-keyed
wake still works for loops that never set ``correlate``.

Determinism: loops are evaluated in ascending ``name`` order; the first loop
whose channel trigger fully matches (structural filters, then optional
semantic condition) wins.

Queueing seam: the constructor accepts an optional
``enqueue: Callable[[str, dict], None] | None`` callback, wired to the real
queue module in normal operation. When it is ``None`` and the decision would
be "queue" (single concurrency, an active run already exists), the matcher
does NOT consume the message — nothing would drain a queue that doesn't
exist — it logs a warning and lets the message fall through as a normal turn
instead.

``durin.loops.hooks.HookDispatcher`` (webhook trigger ingress) shares this
matcher's wake and fire/queue decision instead of re-implementing the
pending-fires race guard: it calls ``_try_wake`` and ``_dispatch_match``
directly on a live ``TriggerMatcher`` instance with a synthetic
``InboundMessage``/``InboundFacts`` pair (``channel="webhook"``). Both
methods are private by convention, not by package boundary — a change to
either one's contract must be checked against ``durin/loops/hooks.py`` too.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Awaitable, Callable

from loguru import logger

from durin.agent.tools._telemetry import emit_tool_event
from durin.loops import channel_meta, claims, run_log, store
from durin.loops.channel_meta import InboundFacts
from durin.loops.runtime import LoopBusy, LoopsRuntime
from durin.loops.spec import LoopSpec, LoopTrigger
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
        # Strong refs to in-flight fire/answer tasks: asyncio.create_task only
        # holds a weak reference, so an unreferenced task can be garbage
        # collected mid-run. Discarded via the done callback once finished.
        self._tasks: set[asyncio.Task] = set()

    async def handle_inbound(self, msg: Any) -> bool:
        """Return True when the message was consumed by a loop (claim wake
        or trigger match) and should NOT be dispatched as a normal agent
        turn. Any channel `channel_meta.extract` recognizes is evaluated;
        others (cli, websocket, cron, ...) pass through untouched."""
        facts = channel_meta.extract(msg)
        if facts is None:
            return False

        loops = sorted(store.list_loops(self._ws), key=lambda s: s.name)

        # Custom correlate keys are tried before the plain thread_key (see
        # module docstring for the precedence rationale).
        for spec in loops:
            if not spec.enabled:
                continue
            for trigger in spec.triggers:
                if trigger.source != "channel" or trigger.channel != msg.channel or not trigger.correlate:
                    continue
                custom_key = self._correlate_key(spec.name, trigger, facts)
                if custom_key and await self._try_wake(custom_key, msg):
                    return True

        if facts.thread_key and await self._try_wake(facts.thread_key, msg):
            return True

        for spec in loops:
            if not spec.enabled:
                continue
            trigger = await self._trigger_matches(spec, facts, msg)
            if trigger is None:
                continue
            custom_key = self._correlate_key(spec.name, trigger, facts)
            thread = custom_key if custom_key is not None else facts.thread_key
            return self._dispatch_match(spec, facts, thread, msg) != "passed_busy"

        return False

    @staticmethod
    def _correlate_key(loop_name: str, trigger: LoopTrigger, facts: InboundFacts) -> str | None:
        """Derive the custom claim key for a trigger's ``correlate`` pattern
        against this message, or None if unset/no match. The pattern is
        operator-authored trusted config (parse-time validated to have
        exactly one capture group), but the searched text is still capped at
        2000 chars to bound match cost against arbitrarily long messages."""
        if not trigger.correlate:
            return None
        haystack = (facts.title or "") + "\n" + facts.text[:2000]
        match = re.search(trigger.correlate, haystack)
        if not match:
            return None
        return f"custom:{loop_name}:{match.group(1)}"

    async def _try_wake(self, key: str, msg: Any) -> bool:
        claim = claims.lookup(self._ws, key)
        if not claim:
            return False
        loop_name = claim.get("loop")
        run_id = claim.get("run_id")
        record = run_log.read_run(self._ws, loop_name, run_id) if loop_name and run_id else None
        if record and record.get("status") == "waiting_info":
            if not self._wants_wake(loop_name, msg.channel):
                # The claim-holder loop declared match: "always_new" on this
                # channel — it wants every matching message to open its own
                # run, not resume this one. Leave the claim in place (the run
                # is still genuinely waiting) and let the message fall
                # through to normal trigger matching instead.
                return False
            self._emit(loop_name, msg.channel, "woke")
            self._track(asyncio.create_task(self._answer(loop_name, run_id, msg.content, key)))
            return True
        # Stale claim: the run moved on (or vanished) without releasing it.
        claims.release(self._ws, key)
        return False

    def _wants_wake(self, loop_name: str, channel: str) -> bool:
        """False only when the claim-holder loop's own channel trigger for
        this channel explicitly declares match: "always_new". A missing loop
        spec or missing matching trigger (deleted loop, cron-only loop)
        defaults to True — the claim exists, so honoring it is the safe
        default."""
        try:
            spec = store.load_loop(self._ws, loop_name)
        except Exception:
            return True
        for trigger in spec.triggers:
            if trigger.source == "channel" and trigger.channel == channel:
                return trigger.match != "always_new"
        return True

    async def _trigger_matches(self, spec: LoopSpec, facts: InboundFacts, msg: Any) -> LoopTrigger | None:
        for trigger in spec.triggers:
            if trigger.source != "channel" or trigger.channel != msg.channel:
                continue
            if not self._structural_match(trigger.filters, facts):
                continue
            if trigger.semantic and not await self._semantic_match(trigger.semantic, facts, msg):
                continue
            return trigger
        return None

    @staticmethod
    def _structural_match(filters: dict, facts: InboundFacts) -> bool:
        # from_contains and sender_contains are independent aliases that both
        # filter on facts.sender; either (or both) may be set on a trigger.
        for key in ("from_contains", "sender_contains"):
            needle = filters.get(key)
            if needle and needle.lower() not in facts.sender.lower():
                return False
        subject_needle = filters.get("subject_contains")
        if subject_needle:
            if facts.title is None or subject_needle.lower() not in facts.title.lower():
                return False
        text_needle = filters.get("text_contains")
        if text_needle and text_needle.lower() not in facts.text.lower():
            return False
        return True

    async def _semantic_match(self, condition: str, facts: InboundFacts, msg: Any) -> bool:
        if self._semantic_judge is None:
            logger.warning(
                "loops: trigger has a semantic condition but no semantic_judge is "
                "configured; treating as no-match"
            )
            return False
        lines = [f"From: {facts.sender}"]
        if facts.title is not None:
            lines.append(f"Subject: {facts.title}")
        summary = "\n".join(lines) + f"\n\n{facts.text[:_SUMMARY_CONTENT_CHARS]}"
        try:
            return bool(await self._semantic_judge(condition, summary))
        except Exception:
            logger.warning("loops: semantic_judge raised for condition {!r}; treating as no-match", condition)
            return False

    def _dispatch_match(self, spec: LoopSpec, facts: InboundFacts, thread: str | None, msg: Any) -> str:
        """Decide fire vs queue for a matched trigger and schedule/enqueue it.

        Returns the action taken: "fired" (a fire task was scheduled — the
        actual runtime.fire() outcome isn't known yet), "queued" (single
        concurrency, an active/pending run already exists, event pushed to
        the per-loop queue), or "passed_busy" (busy AND no queue wired — the
        message was NOT consumed). See the module docstring for
        ``HookDispatcher``'s reuse of this method.
        """
        origin = {"channel": msg.channel, "sender": facts.sender, "chat_id": msg.chat_id,
                  "thread": thread, "subject": facts.title, "reply": facts.reply}
        busy = spec.concurrency != "parallel" and (
            spec.name in self._pending_fires or bool(run_log.active_runs(self._ws, spec.name))
        )
        if not busy:
            self._pending_fires.add(spec.name)
            self._track(asyncio.create_task(self._fire(spec.name, msg.channel, msg.content, origin)))
            return "fired"
        if self._enqueue is not None:
            self._emit(spec.name, msg.channel, "queued")
            self._enqueue(spec.name, self._queue_event(msg.content, origin))
            return "queued"
        logger.warning(
            "loops: loop '{}' matched but is busy (single-concurrency) and no queue "
            "is wired; passing the message through as a normal turn", spec.name,
        )
        self._emit(spec.name, msg.channel, "passed_busy")
        return "passed_busy"

    def _track(self, task: asyncio.Task) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

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

    async def _answer(self, loop_name: str, run_id: str, content: str, key: str) -> None:
        try:
            await self._runtime.answer(loop_name, run_id, content)
        except Exception:
            # runtime.answer() releases the claim itself once it gets far
            # enough (release-before-resume), but a failure before that
            # point — e.g. the loop spec was deleted between the wake
            # decision and this task running — leaves it stuck otherwise.
            # release() is idempotent, so this is a no-op in the common case
            # where the claim is already gone.
            logger.exception("loops: answer('{}', '{}') failed; releasing claim", loop_name, run_id)
            claims.release(self._ws, key)

    def _emit(self, loop_name: str, channel: str, action: str) -> None:
        token = None
        if current_telemetry() is None:
            token = bind_telemetry(get_session_logger(f"loop:{loop_name}"))
        try:
            emit_tool_event("loops.event_matched", {"loop": loop_name, "source_channel": channel, "action": action})
        finally:
            if token is not None:
                reset_telemetry(token)
