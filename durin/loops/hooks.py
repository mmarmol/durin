"""Webhook trigger ingress: matches an inbound ``POST /api/v1/hooks/{hook}``
against loops' webhook triggers and dispatches through the same wake/fire/
queue machinery ``TriggerMatcher`` uses for channel messages.

Seam with TriggerMatcher: rather than re-implementing the pending-fires race
guard and the busy/single-concurrency queue decision, ``dispatch`` builds a
synthetic ``InboundMessage``/``InboundFacts`` pair with ``channel="webhook"``
and calls ``TriggerMatcher._correlate_key``, ``TriggerMatcher._try_wake``
(wake pass), ``TriggerMatcher._semantic_match`` (fire pass, via
``_matching_trigger``) and ``TriggerMatcher._dispatch_match`` (fire/queue)
directly on the live matcher instance passed at construction, reading its
``_ws`` workspace handle for the wake pass's own ``claims.lookup``. All are
private by convention only, not by package boundary — this module and
``durin.loops.matcher`` are siblings expected to evolve together; a change to
any of these methods' contracts must be checked against this file too.

Two-pass dispatch, mirroring ``TriggerMatcher.handle_inbound``'s own
claim-wake-then-trigger-match structure (matcher.py:106-131): the WAKE pass
(``_wake_pass``) tries every enabled loop's matching webhook trigger with a
``correlate`` that captures on the payload, purely structurally — it never
evaluates a trigger's ``semantic`` condition, because a claim already exists
for a run that is genuinely waiting and the semantic condition only gates
whether a *new* run should be opened. Only if no claim wakes does the FIRE
pass run, where ``semantic`` (fail-closed when unconfigured or erroring) is
the entry gate before ``_dispatch_match`` decides fire vs queue.

Wake policy: ``_try_wake``'s claim-holder check (``_wants_wake``) only
special-cases channel triggers declaring ``match: "always_new"``; a webhook
trigger has no ``match`` field (see ``durin.loops.spec``), so
``_wants_wake`` always falls through to its default ``True`` for a
webhook-origin message — exactly the "wake always when a claim exists"
contract webhook triggers get, with no extra branching needed here.

Two ``InboundFacts`` are built per candidate trigger, not one: the
wake/correlate/semantic pass uses ``title=None`` so the searched text is the
payload text alone (an email-style ``"<hook>\\n"`` prefix would break a
``^``-anchored correlate regex or pollute the semantic-judge summary); the
fire/queue pass uses ``title=hook`` so ``_dispatch_match``'s origin
construction (``subject: facts.title``) satisfies the webhook fire
contract's ``subject: hook``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from durin.bus.events import InboundMessage
from durin.loops import claims, store
from durin.loops.channel_meta import InboundFacts
from durin.loops.spec import LoopSpec, LoopTrigger

if TYPE_CHECKING:
    from durin.loops.matcher import TriggerMatcher

_TEXT_CAP = 4000


def _payload_text(payload: dict) -> str:
    """``payload["text"]`` verbatim if it's a non-empty string, else the
    whole payload compacted to JSON — either way capped at 4000 chars so an
    oversized webhook body doesn't balloon run manifests/queue files."""
    text = payload.get("text")
    if isinstance(text, str) and text:
        return text[:_TEXT_CAP]
    return json.dumps(payload, separators=(",", ":"))[:_TEXT_CAP]


def _facts_for_match(hook: str, text: str) -> InboundFacts:
    return InboundFacts(sender=hook, text=text, title=None, thread_key=None, reply={})


def _facts_for_origin(hook: str, text: str) -> InboundFacts:
    return InboundFacts(sender=hook, text=text, title=hook, thread_key=None, reply={})


class HookDispatcher:
    """Matches webhook POSTs against enabled loops' webhook triggers."""

    def __init__(self, matcher: "TriggerMatcher") -> None:
        self._matcher = matcher
        self._ws = matcher._ws

    async def dispatch(self, hook: str, payload: dict) -> dict:
        """Route one webhook POST in two passes (see module docstring): a
        WAKE pass that never evaluates ``semantic``, then — only if nothing
        woke — a FIRE pass where ``semantic`` gates entry. Loops are
        evaluated in ascending ``name`` order in each pass; the first match
        wins."""
        text = _payload_text(payload)
        msg = InboundMessage(channel="webhook", sender_id=hook, chat_id=hook, content=text)
        match_facts = _facts_for_match(hook, text)

        loops = sorted(store.list_loops(self._ws), key=lambda s: s.name)

        woken = await self._wake_pass(hook, loops, match_facts, msg)
        if woken is not None:
            return woken

        for spec in loops:
            if not spec.enabled:
                continue
            trigger = await self._matching_trigger(spec, hook, match_facts, msg)
            if trigger is None:
                continue

            custom_key = self._matcher._correlate_key(spec.name, trigger, match_facts)
            origin_facts = _facts_for_origin(hook, text)
            action = self._matcher._dispatch_match(spec, origin_facts, custom_key, msg)
            if action == "passed_busy":
                # Only reachable if the shared matcher has no queue wired — a
                # wiring bug in production (commands.py always wires one via
                # durin.loops.queue.push). The message wasn't consumed;
                # report it like no loop matched rather than inventing a
                # fifth result value outside the documented contract.
                return {"result": "no_match"}
            return {"result": action, "loop": spec.name}

        return {"result": "no_match"}

    async def _wake_pass(
        self, hook: str, loops: list[LoopSpec], match_facts: InboundFacts, msg: InboundMessage
    ) -> dict | None:
        """Try to resume a waiting run via a correlate-derived claim key,
        structurally only — never evaluates a trigger's ``semantic``
        condition. Mirrors ``TriggerMatcher.handle_inbound``'s own
        claim-wake loop (matcher.py:106-118), which the module docstring
        there documents as deliberately semantic-blind: the condition gates
        whether a *new* run opens, not whether an already-waiting one may
        resume. Returns the "woken" result dict, or None if nothing woke."""
        for spec in loops:
            if not spec.enabled:
                continue
            for trigger in spec.triggers:
                if trigger.source != "webhook" or trigger.hook != hook:
                    continue
                custom_key = self._matcher._correlate_key(spec.name, trigger, match_facts)
                if not custom_key:
                    continue
                claim = claims.lookup(self._ws, custom_key)
                if claim and await self._matcher._try_wake(custom_key, msg):
                    result = {"result": "woken", "loop": spec.name}
                    if claim.get("run_id"):
                        result["run_id"] = claim["run_id"]
                    return result
        return None

    async def _matching_trigger(
        self, spec: LoopSpec, hook: str, match_facts: InboundFacts, msg: InboundMessage
    ) -> LoopTrigger | None:
        for trigger in spec.triggers:
            if trigger.source != "webhook" or trigger.hook != hook:
                continue
            if trigger.semantic and not await self._matcher._semantic_match(
                trigger.semantic, match_facts, msg
            ):
                continue
            return trigger
        return None
