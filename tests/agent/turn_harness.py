"""Many turns of the real AgentLoop, drawn from a seed, and the properties
every turn must keep whatever happened in it.

A scenario is generated from a seed: the window and output ceiling of the
loop's model, its compaction ratio and cap, a context_block_limit, the size
of AGENTS.md, every message from a few words to near the budget, and what
happens between and inside turns: queued and steer messages, sub-agent and
workflow results, /new, /compact, a persona with its own model, a cron-style
turn on another model (and a sub-agent result landing after it), the
task-state tools, a provider that errors, overflows or answers empty, the
"try again" after it, a compaction that cannot run, the nightly
session-summary pass. The provider is scripted call by call and stamps
every answer with what a real provider reports: the token count of the
request as sent. A seed runs the same in any checkout or temporary
directory.

The driver runs each turn the way the gateway does (the turn task with its
pending queues, leftovers re-published to the bus and run as turns of their
own, the compaction check scheduled after SAVE run before the next turn) and
records every request the provider received, every prompt the runner was
given, every compaction check, and what the turn left on disk.
``check_record`` reads those after every turn and ``check_scenario`` once at
the end; each failed check is a ``Violation`` naming the invariant, the seed
and the turn. ``python -m tests.agent.turn_harness <seed>`` replays a seed
and prints what it found.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import math
import os
import random
import re
import sys
import tempfile
from collections import Counter, OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import patch

import tiktoken
import yaml

import durin.agent.context as context_module
import durin.agent.skills as skills_module
import durin.memory.session_summary_store as session_summary_store
from durin.agent.loop import AgentLoop
from durin.agent.memory import Consolidator
from durin.agent.runner import (
    _PERSISTED_MODEL_ERROR_PLACEHOLDER,
    _PERSISTED_OVERFLOW_PLACEHOLDER,
    input_budget_tokens,
    provider_max_output,
)
from durin.bus.events import InboundMessage
from durin.bus.queue import MessageBus
from durin.config.schema import Config, ModelPresetConfig, PersonaConfig
from durin.memory.eager_surface import SNAPSHOT_KEY
from durin.memory.session_summary_dream import summarize_session
from durin.memory.session_summary_store import get_session_summary, write_session_summary
from durin.providers.base import GenerationSettings, LLMProvider, LLMResponse, ToolCallRequest
from durin.providers.factory import ProviderSnapshot
from durin.souls.store import SoulStore
from durin.utils.helpers import (
    estimate_message_tokens,
    estimate_prompt_tokens,
    estimate_text_tokens,
)
from durin.utils.prompt_templates import render_template
from durin.utils.runtime import EMPTY_FINAL_RESPONSE_MESSAGE, NO_ROOM_PLACEHOLDER

KEY = "cli:sim"
CHANNEL = "cli"
CHAT_ID = "sim"
LOOP_MODEL = "loop-model"

# What a turn that produced no answer leaves in the session in its place.
FAILURE_NOTICES = (
    _PERSISTED_MODEL_ERROR_PLACEHOLDER,
    _PERSISTED_OVERFLOW_PLACEHOLDER,
    NO_ROOM_PLACEHOLDER,
    EMPTY_FINAL_RESPONSE_MESSAGE,
)
# How a turn ends when it delivered no answer of the model's.
FAILED_STOP_REASONS = frozenset({
    "error", "mid_turn_precheck_overflow", "empty_final_response", "tool_error",
    "circuit_breaker_idle_timeout", "unknown_tool_loop_guard", "post_compaction_loop",
})

USER_MARK = re.compile(r"\[\[u\d+\]\]")
REPLY_MARK = re.compile(r"\[\[r\d+\]\]")
SUMMARY_BLOCK = "\n\n---\n\n[Archived Context Summary]"
PREVIOUS_SESSION = "=== PREVIOUS SESSION SUMMARY"
PROBE_MESSAGE = "[token-probe]"
# The head block a stored summary carries the paths of its evicted blocks in.
CARRIED_PATHS = "Files/paths from earlier spans (evicted): "
SUMMARY_BLOCK_SEP = "\n\n---\n"
DECISIONS = re.compile(r"## Decisions & findings\n(?:  - [^\n]*(?:\n|$))*")
ARCHIVE_LINE = re.compile(r"^\[[^\]\n]{0,16}\] (?:USER|ASSISTANT|TOOL|SYSTEM)", re.MULTILINE)
TRUNCATED = "... (truncated)"
NIGHTLY_OMITTED = "(earlier turns omitted)"

# A message of at least this many tokens may move the summary cut the
# system prompt carries; below it the cut does not follow the message.
MESSAGE_ALLOWANCE = 2_048

# The precheck estimate a request was sized by may differ from the request
# as sent by this much. The estimate adds up parts (the latest usage stamp,
# the messages after it, the task state appended) that a provider counts as
# one text, and tiktoken joins neighbouring parts a token or two apart; a
# count that left out or doubled a part is off by hundreds or more.
ESTIMATE_TOLERANCE = 32

# What the build may leave between the parts of a turn that cannot shrink
# (system prompt without its summary, tool definitions, the turn's message
# without its decision log) and the input budget, and still fail the turn.
FIT_MARGIN = 256
FIT_MARGIN_SHARE = 0.01

_VOCAB = (
    "service deploy config port vault path build test release branch merge review cache "
    "prompt token window budget summary session turn reply request model provider retry "
    "error queue steer result workflow agent tool file folder note decision goal task "
    "plan check limit output input history migration database schema index backup "
    "restore monitor alert metric latency cluster node shard replica network proxy route"
).split()


def filler(rng: random.Random, tokens: int) -> str:
    """*tokens* tokens of plain sentences (as tiktoken counts them),
    different on every call."""
    tokens = max(1, tokens)
    words: list[str] = []
    while len(words) < tokens + 8:
        words.extend(rng.choice(_VOCAB) for _ in range(rng.randint(6, 14)))
        words.append(f"item {rng.randint(100, 9999)}.")
    encoding = tiktoken.get_encoding("cl100k_base")
    text = encoding.decode(encoding.encode(" ".join(words))[:tokens]).strip()
    return text[0].upper() + text[1:]


def text_of(message: dict[str, Any] | None) -> str:
    if not message:
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict))
    return ""


def joined_text(messages: list[dict[str, Any]]) -> str:
    return "\n".join(text_of(m) for m in messages)


# ---------------------------------------------------------------------------
# What the harness changes around the loop, and why
# ---------------------------------------------------------------------------


class _ParsedOnceYaml:
    """``yaml`` as the skills loader uses it, with each frontmatter parsed once.

    Every prompt build lists the skills and parses each SKILL.md frontmatter
    again: about half of a turn's local time. Scenarios never write a skill,
    so the same text always parses the same; the copy keeps one caller's
    edit from reaching another."""

    YAMLError = yaml.YAMLError

    def __init__(self) -> None:
        self._parsed: dict[str, tuple[bool, Any]] = {}

    def safe_load(self, text: Any) -> Any:
        if not isinstance(text, str):
            return yaml.safe_load(text)
        if text not in self._parsed:
            try:
                self._parsed[text] = (True, yaml.safe_load(text))
            except yaml.YAMLError as exc:
                self._parsed[text] = (False, exc)
        ok, value = self._parsed[text]
        if not ok:
            raise value
        return copy.deepcopy(value)

    def __getattr__(self, name: str) -> Any:
        return getattr(yaml, name)


_PARSED_ONCE = _ParsedOnceYaml()


class _RememberedEncode:
    """``Encoding.encode`` that remembers the tokens of the long texts it saw
    last. A turn counts the same tool definitions, system prompt tiers and
    AGENTS.md dozens of times (every build, probe, precheck and trim); the
    tokens of a text never change, and the count is the same."""

    _MIN_CHARS = 2_000
    _SIZE = 96

    def __init__(self, real: Callable[..., list[int]]) -> None:
        self._real = real
        self._seen: OrderedDict[tuple[Any, ...], list[int]] = OrderedDict()

    @staticmethod
    def _special(value: Any) -> Any:
        return value if isinstance(value, str) else tuple(sorted(value))

    def __call__(self, text: str, *, allowed_special: Any = frozenset(), disallowed_special: Any = "all") -> list[int]:
        if len(text) < self._MIN_CHARS:
            return self._real(text, allowed_special=allowed_special, disallowed_special=disallowed_special)
        key = (text, self._special(allowed_special), self._special(disallowed_special))
        tokens = self._seen.get(key)
        if tokens is None:
            tokens = self._real(text, allowed_special=allowed_special, disallowed_special=disallowed_special)
            self._seen[key] = tokens
            if len(self._seen) > self._SIZE:
                self._seen.popitem(last=False)
        else:
            self._seen.move_to_end(key)
        return list(tokens)


@contextlib.contextmanager
def harness_patches():
    """A fixed clock in the runtime context, so a seed sizes its prompts the
    same on every run; the skill frontmatter parsed once per text, and long
    texts tokenized once, so a scenario costs what its prompts cost."""
    encoding = tiktoken.get_encoding("cl100k_base")
    with patch.object(context_module, "current_time_str", lambda *_a, **_k: "2026-10-01 12:00 (Thursday) (UTC)"), \
            patch.object(skills_module, "yaml", _PARSED_ONCE), \
            patch.object(encoding, "encode", _RememberedEncode(encoding.encode)):
        yield


# ---------------------------------------------------------------------------
# Scenario
# ---------------------------------------------------------------------------


@dataclass
class Action:
    """What the scripted model does on one call of a turn.

    ``kind``: ``answer`` (a reply of about ``size`` tokens), ``tool`` (a call
    to ``tool`` with ``args``), ``error`` (the provider fails), ``overflow``
    (the provider refuses the prompt as too long), ``empty`` (a blank reply).
    ``inject`` holds messages that arrive while this call runs:
    ``(queue, marker text)`` with queue ``queued``, ``steer`` or ``subagent``."""

    kind: str = "answer"
    size: int = 200
    tool: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    inject: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class TurnPlan:
    """One turn of a scenario.

    ``kind``: ``user`` (a message from the user, also ``/new`` and
    ``/compact``), ``subagent`` or ``workflow`` (a background result landing
    on the session), ``cron`` (a direct turn on the session given its own
    model, as a cron job with a session key runs), ``nightly`` (the dream's
    session-summary pass over the session gone idle). ``before`` holds what
    changes before the turn: ``("agents_md", words)``, ``("persona", name or
    None)``. ``stall`` makes every compaction check of the turn return without
    compacting, as one whose lock timed out does."""

    kind: str = "user"
    text: str = ""
    actions: list[Action] = field(default_factory=list)
    reply_size: int = 200
    before: list[tuple[str, Any]] = field(default_factory=list)
    stall: bool = False
    model_preset: str | None = None
    persona: str | None = None


@dataclass
class ModelSpec:
    """A model preset: the loop's own (``default``), a persona's or a cron
    job's."""

    name: str
    model: str
    window: int
    max_out: int
    ratio: float | None = None
    cap: int | None = None


@dataclass
class Scenario:
    seed: int
    profile: str
    loop_model: ModelSpec
    ratio: float = 0.5
    cap: int | None = 256_000
    block_limit: int | None = None
    max_messages: int = 480
    agents_words: int = 0
    summary_tokens: int = 200
    decisions_per_compaction: int = 1
    presets: list[ModelSpec] = field(default_factory=list)
    personas: dict[str, tuple[int, str | None]] = field(default_factory=dict)
    seed_history: list[dict[str, Any]] = field(default_factory=list)
    seed_summary_chars: int = 0
    # Paths the seeded summary carries from blocks the store evicted.
    seed_carried_paths: int = 0
    data_file_tokens: int = 0
    # Small files notes/file<i>.txt for read_file calls to name.
    small_files: int = 0
    # The model the dream's nightly pass summarizes with, when not the
    # loop's own (a memory preset of its own): it sizes the pass's calls.
    dream_model: ModelSpec | None = None
    turns: list[TurnPlan] = field(default_factory=list)

    def describe(self) -> str:
        m = self.loop_model
        return (
            f"seed={self.seed} profile={self.profile} window={m.window} max_out={m.max_out} "
            f"ratio={self.ratio} cap={self.cap} block_limit={self.block_limit} "
            f"max_messages={self.max_messages} agents_words={self.agents_words} "
            f"turns={len(self.turns)} presets={[(p.name, p.window) for p in self.presets]}"
            + (f" dream=({self.dream_model.window}, {self.dream_model.max_out})" if self.dream_model else "")
        )


# ---------------------------------------------------------------------------
# What a run records
# ---------------------------------------------------------------------------


@dataclass
class Request:
    """One request the provider received, measured as sent."""

    kind: str  # main | archive | decisions | learnings | other
    model: str
    window: int | None
    tokens: int
    max_tokens: int | None
    default_max: int
    estimate: int | None = None
    user_marks: Counter = field(default_factory=Counter)
    input: str = ""


@dataclass
class Attempt:
    """One run the loop handed to the runner: the prompt it started from."""

    messages: list[dict[str, Any]]
    model: str
    window: int | None
    block_limit: int | None
    max_out: int
    tools: list[dict[str, Any]]
    summary: str | None
    # The eager memory surface the build used (when it was frozen), or None
    # for one rendered live.
    surface: str | None = None
    stop_reason: str = ""
    # Requests with tools the run made.
    calls: int = 0
    # Requests the precheck found over the budget: (its estimate, the
    # budget, the request it would have sent, counted as sent).
    refusals: list[tuple[int, int, int]] = field(default_factory=list)

    @property
    def budget(self) -> int | None:
        return input_budget_tokens(self.window, self.max_out, self.block_limit)

    def history_free(self, *, compressible: bool) -> int:
        """Tokens of the prompt with its history left out: the system prompt,
        the turn's own message and the tool definitions. Without
        ``compressible`` the parts a build may cut to make the turn fit (the
        summary and the decision log) are left out too."""
        system = text_of(self.messages[0]) if self.messages and self.messages[0].get("role") == "system" else ""
        current = self.messages[-1] if self.messages else {"role": "user", "content": ""}
        current_text = text_of(current)
        if not compressible:
            cut = system.find(SUMMARY_BLOCK)
            system = system if cut < 0 else system[:cut]
            current_text = DECISIONS.sub("", current_text)
        return estimate_prompt_tokens(
            [{"role": "system", "content": system}, {"role": current.get("role", "user"), "content": current_text}],
            self.tools or None,
        )

    @property
    def message_tokens(self) -> int:
        return estimate_prompt_tokens([self.messages[-1]]) if self.messages else 0


@dataclass
class Check:
    """One compaction check: where it ran, whether it was forced by an
    overflow, and how far it moved the session's cursor."""

    phase: str
    force: bool
    before: int
    after: int
    stalled: bool

    @property
    def compacted(self) -> bool:
        return self.after > self.before


@dataclass
class TurnRecord:
    index: int
    plan_index: int
    kind: str
    marker: str | None
    events: set[str]
    stalled: bool
    planned_calls: int
    attempts: list[Attempt] = field(default_factory=list)
    requests: list[Request] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    injected: list[str] = field(default_factory=list)
    reply: str | None = None
    stop_reason: str | None = None
    crashed: str | None = None
    session: list[dict[str, Any]] = field(default_factory=list)
    session_before: int = 0
    last_consolidated: int = 0
    growth: int = 0
    # Changes of the summary the system prompt carries since the latest
    # change one of its inputs explains.
    prompt_changes: int = 0
    # The system prompt the compaction check before the turn's build
    # measured, and the stored summary it read.
    probe_system: str | None = None
    probe_summary: str | None = None
    # Scans of the summary store for the previous session's summary while
    # the turn ran, and in the compaction check scheduled after it.
    scans: int = 0
    background_scans: int = 0


@dataclass
class Violation:
    invariant: str
    seed: int
    record: int
    plan_turn: int
    detail: str

    def __str__(self) -> str:
        return f"[{self.invariant}] seed={self.seed} turn={self.record} (plan turn {self.plan_turn}): {self.detail}"


# ---------------------------------------------------------------------------
# The provider
# ---------------------------------------------------------------------------


class ScriptedProvider(LLMProvider):
    """A provider whose answers the driver scripts call by call. A request
    reaches it the way one reaches a real provider once that provider's own
    retries are spent."""

    def __init__(self, driver: TurnDriver, model: str, max_tokens: int) -> None:
        super().__init__()
        self._driver = driver
        self._model = model
        self.generation = GenerationSettings(max_tokens=max_tokens)

    def get_default_model(self) -> str:
        return self._model

    async def chat(self, messages, tools=None, model=None, **kwargs):  # type: ignore[override]
        return await self._driver.answer(self, messages=messages, tools=tools, model=model, **kwargs)

    async def chat_with_retry(self, **kwargs):  # type: ignore[override]
        return await self._driver.answer(self, **kwargs)

    async def chat_stream_with_retry(self, **kwargs):  # type: ignore[override]
        kwargs.pop("on_content_delta", None)
        kwargs.pop("on_thinking_delta", None)
        return await self._driver.answer(self, **kwargs)


def _side_prompts() -> dict[str, str]:
    return {
        render_template("agent/consolidator_archive.md", strip=True): "archive",
        render_template("agent/consolidator_decisions.md", strip=True): "decisions",
        render_template("agent/consolidator_learnings.md", strip=True): "learnings",
    }


# ---------------------------------------------------------------------------
# The driver
# ---------------------------------------------------------------------------


class TurnDriver:
    def __init__(self, scenario: Scenario, workspace: Path) -> None:
        self.scenario = scenario
        self.workspace = workspace
        self.rng = random.Random(scenario.seed * 7919 + 17)
        self.records: list[TurnRecord] = []
        self.record: TurnRecord | None = None
        self.violations: list[Violation] = []
        # Every user message the scenario sent or seeded: marker -> text.
        self.user_texts: dict[str, str] = {}
        # Every reply the model produced, in order: (mark, record index).
        self.replies: list[tuple[str, int]] = []
        # Every text a summarizing call received, with the input budget of
        # the call: compaction's (the loop model's) or the nightly pass's.
        self.summarized: list[tuple[str, int]] = []
        # Sub-agent results that landed between turns: (text, record index).
        self.system_results: list[tuple[str, int]] = []
        self.windows: dict[str, int] = {}
        self.new_at = 0  # the first record after the latest /new
        self.phase = "turn"
        self.stalled = False
        self._actions: list[Action] = []
        self._reply_size = 200
        self._estimate: int | None = None
        self._events: set[str] = set()
        self._background: list[Any] = []
        self._calls = 0
        self._summaries = 0
        self._side_prompts = _side_prompts()
        self._archive_prompt = render_template("agent/consolidator_archive.md", strip=True)
        self.loop: AgentLoop | None = None
        self.summarizer_budget = 0
        # The nightly pass's calls: the model it summarizes with, and the
        # input budget the dream gives it.
        self.nightly_model = LOOP_MODEL
        self.nightly_window = 0
        self.nightly_max_out = 0
        self.nightly_budget = 0
        self.base = 0

    # -- building the loop ---------------------------------------------------

    def _preset(self, spec: ModelSpec) -> ModelPresetConfig:
        fields: dict[str, Any] = {
            "model": spec.model, "context_window_tokens": spec.window, "max_tokens": spec.max_out,
        }
        if spec.ratio is not None:
            fields["preemptive_compact_ratio"] = spec.ratio
        if spec.cap is not None:
            fields["preemptive_compact_max_tokens"] = spec.cap
        return ModelPresetConfig(**fields)

    def _build(self) -> AgentLoop:
        sc = self.scenario
        presets = {"default": self._preset(sc.loop_model)}
        self.windows[sc.loop_model.model] = sc.loop_model.window
        for spec in sc.presets:
            presets[spec.name] = self._preset(spec)
            self.windows[spec.model] = spec.window
        config = app_config()
        for name, (soul_words, model) in sc.personas.items():
            config.personas[name] = PersonaConfig(soul=name, model=model)
            SoulStore(self.workspace).write(name, f"You are {name}. " + filler(self.rng, soul_words))

        def preset_loader(name: str, preset: ModelPresetConfig | None = None) -> ProviderSnapshot:
            # As the gateway's loader does: a provider of the preset's own.
            preset = preset or presets[name]
            return ProviderSnapshot(
                provider=ScriptedProvider(self, preset.model, preset.max_tokens),
                model=preset.model,
                context_window_tokens=preset.context_window_tokens,
                signature=("model_preset", name, preset.model_dump_json()),
                preemptive_compact_ratio=preset.preemptive_compact_ratio,
                preemptive_compact_max_tokens=preset.preemptive_compact_max_tokens,
            )

        loop = AgentLoop(
            bus=MessageBus(),
            provider=ScriptedProvider(self, sc.loop_model.model, sc.loop_model.max_out),
            workspace=self.workspace,
            model=sc.loop_model.model,
            context_window_tokens=sc.loop_model.window,
            context_block_limit=sc.block_limit,
            max_messages=sc.max_messages,
            preemptive_compact_ratio=sc.ratio,
            preemptive_compact_max_tokens=sc.cap,
            model_presets=presets,
            preset_snapshot_loader=preset_loader,
            app_config=config,
        )
        # What this workspace's prompt holds before AGENTS.md: the system
        # prompt names the workspace and the skills by absolute path, so it
        # differs by a few tokens from one checkout or temporary directory to
        # another; AGENTS.md makes up the difference (see reference_base).
        self.base = self._fixed_tokens(loop)
        self._write_agents_md(sc.agents_words, loop)
        self._hook(loop)
        self.summarizer_budget = loop.consolidator._input_token_budget
        dream = sc.dream_model
        if dream is None:
            # By default the memory preset is the loop's own model: the pass
            # is sized as compaction is.
            self.nightly_window, self.nightly_max_out = sc.loop_model.window, sc.loop_model.max_out
            self.nightly_budget = self.summarizer_budget
        else:
            self.nightly_model = dream.model
            self.nightly_window, self.nightly_max_out = dream.window, dream.max_out
            self.nightly_budget = dream.window - dream.max_out - Consolidator._SAFETY_BUFFER
        return loop

    def _hook(self, loop: AgentLoop) -> None:
        driver = self

        def schedule(coro: Any) -> None:
            # A quiet gateway runs the check scheduled after SAVE (and the
            # /new record) before the next turn arrives; the reindex and the
            # rest are not part of what these checks watch.
            name = getattr(getattr(coro, "cr_code", None), "co_name", "")
            if name in ("maybe_consolidate_by_tokens", "_archive_closed_session"):
                driver._background.append(coro)
            else:
                coro.close()

        loop._schedule_background = schedule  # type: ignore[method-assign]

        real_run = loop.runner.run

        async def run(spec: Any) -> Any:
            snapshot = loop.sessions.get_or_create(KEY).metadata.get(SNAPSHOT_KEY)
            attempt = Attempt(
                messages=copy.deepcopy(spec.initial_messages),
                model=spec.model,
                window=spec.context_window_tokens,
                block_limit=spec.context_block_limit,
                max_out=provider_max_output(spec.provider or loop.runner.provider),
                tools=list(spec.tools.get_definitions()) if spec.tools is not None else [],
                summary=driver._stored_summary(),
                surface=f"{snapshot.get('turn')}@{snapshot.get('frozen_at')}" if isinstance(snapshot, dict) else None,
            )
            driver.record.attempts.append(attempt)
            result = await real_run(spec)
            attempt.stop_reason = result.stop_reason
            return result

        loop.runner.run = run  # type: ignore[method-assign]

        real_effective = loop.runner._effective_max_tokens

        def effective_max_tokens(spec: Any, estimate: int, provider: Any = None) -> Any:
            # The estimate the precheck sized the request it is about to send by.
            driver._estimate = estimate
            return real_effective(spec, estimate, provider)

        loop.runner._effective_max_tokens = effective_max_tokens  # type: ignore[method-assign]

        real_estimate = loop.runner._estimate_and_budget

        def estimate_and_budget(spec: Any, messages: Any, provider: Any = None) -> Any:
            # Over the budget, the request is about to be trimmed or refused:
            # what it would have sent, counted as a provider counts it.
            decision = real_estimate(spec, messages, provider)
            if decision is not None and decision[0] > decision[1] and driver.record.attempts:
                kwargs = loop.runner._build_request_kwargs(
                    spec, messages, tools=loop.runner._active_tool_definitions(spec),
                )
                sent = estimate_prompt_tokens(kwargs["messages"], kwargs.get("tools") or None)
                driver.record.attempts[-1].refusals.append((decision[0], decision[1], sent))
            return decision

        loop.runner._estimate_and_budget = estimate_and_budget  # type: ignore[method-assign]

        real_check = loop.consolidator.maybe_consolidate_by_tokens

        async def maybe_consolidate_by_tokens(session: Any, **kwargs: Any) -> Any:
            check = Check(
                phase=driver.phase, force=bool(kwargs.get("force")),
                before=session.last_consolidated, after=session.last_consolidated, stalled=driver.stalled,
            )
            driver.record.checks.append(check)
            if driver.stalled:
                return None
            try:
                return await real_check(session, **kwargs)
            finally:
                check.after = session.last_consolidated

        loop.consolidator.maybe_consolidate_by_tokens = maybe_consolidate_by_tokens  # type: ignore[method-assign]

        real_build = loop.consolidator._build_messages

        def build_messages(*args: Any, **kwargs: Any) -> Any:
            # The compaction check measures the next prompt with a build of
            # its own; the last one before the turn's build is what it judged.
            messages = real_build(*args, **kwargs)
            record = driver.record
            if kwargs.get("current_message") == PROBE_MESSAGE and driver.phase == "turn" and not record.attempts:
                record.probe_system = text_of(messages[0]) if messages else None
                record.probe_summary = driver._stored_summary()
            return messages

        loop.consolidator._build_messages = build_messages  # type: ignore[method-assign]

        real_process = loop._process_message

        async def process_message(*args: Any, **kwargs: Any) -> Any:
            try:
                return await real_process(*args, **kwargs)
            except Exception as exc:
                driver.record.crashed = f"{type(exc).__name__}: {exc}"
                raise

        loop._process_message = process_message  # type: ignore[method-assign]

    # -- the provider's side -------------------------------------------------

    def _classify(self, messages: list[dict[str, Any]], tools: Any) -> str:
        if tools:
            return "main"
        return self._side_prompts.get(text_of(messages[0]) if messages else "", "other")

    def _usage(self, tokens: int, content: str) -> dict[str, int]:
        return {"prompt_tokens": max(1, tokens), "completion_tokens": estimate_text_tokens(content)}

    def _reply(self, request: Request, size: int) -> LLMResponse:
        self._calls += 1
        mark = f"[[r{self._calls}]]"
        content = f"{mark} {filler(self.rng, size)}"
        self.replies.append((mark, self.record.index))
        return LLMResponse(content=content, finish_reason="stop", usage=self._usage(request.tokens, content))

    async def answer(self, provider: ScriptedProvider, **kwargs: Any) -> LLMResponse:
        messages = list(kwargs.get("messages") or [])
        tools = kwargs.get("tools")
        model = kwargs.get("model") or provider.get_default_model()
        kind = self._classify(messages, tools)
        tokens = estimate_prompt_tokens(messages, tools or None)
        request = Request(
            kind=kind, model=model, window=self.windows.get(model), tokens=tokens,
            max_tokens=kwargs.get("max_tokens"), default_max=provider.generation.max_tokens,
        )
        self.record.requests.append(request)
        if kind == "main":
            request.estimate, self._estimate = self._estimate, None
            request.user_marks = Counter(USER_MARK.findall(joined_text(messages)))
            if self.record.attempts:
                self.record.attempts[-1].calls += 1
        asked = request.max_tokens if request.max_tokens is not None else request.default_max
        if request.window and tokens + asked > request.window:
            # A real provider refuses a request its window cannot hold.
            return LLMResponse(
                content=(
                    f"Error calling LLM: This model's maximum context length is {request.window} tokens. "
                    f"However, you requested {tokens + asked} tokens ({tokens} in the messages, {asked} in "
                    "the completion)."
                ),
                finish_reason="error", error_status_code=400, error_code="context_length_exceeded", usage={},
            )
        if kind == "main":
            return self._main_answer(request)
        if kind == "archive":
            request.input = text_of(messages[-1])
            self.summarized.append((request.input, self.summarizer_budget))
            self._summaries += 1
            content = f"- Span {self._summaries}: " + filler(self.rng, self.scenario.summary_tokens)
            return LLMResponse(content=content, finish_reason="stop", usage=self._usage(tokens, content))
        if kind == "decisions":
            lines = [
                f"- Decided {self._summaries}.{i}: " + filler(self.rng, 25)
                for i in range(self.scenario.decisions_per_compaction)
            ]
            content = "\n".join(lines) or "(none)"
            return LLMResponse(content=content, finish_reason="stop", usage=self._usage(tokens, content))
        if kind == "learnings":
            return LLMResponse(content="[]", finish_reason="stop", usage=self._usage(tokens, "[]"))
        # The no-tools finalization retry after blank answers.
        return self._reply(request, self._reply_size)

    def _main_answer(self, request: Request) -> LLMResponse:
        action = self._actions.pop(0) if self._actions else Action("answer", size=self._reply_size)
        for queue, text in action.inject:
            self._inject(queue, text)
        if action.kind == "answer":
            return self._reply(request, action.size)
        if action.kind == "error":
            return LLMResponse(content="Error calling LLM: 503 upstream overloaded", finish_reason="error", usage={})
        if action.kind == "overflow":
            return LLMResponse(
                content=(
                    f"Error calling LLM: This model's maximum context length is {request.window} tokens. "
                    f"However, your messages resulted in {request.tokens + 4096} tokens."
                ),
                finish_reason="error", error_status_code=400, error_code="context_length_exceeded", usage={},
            )
        if action.kind == "empty":
            return LLMResponse(content="", finish_reason="stop", usage=self._usage(request.tokens, ""))
        self._calls += 1
        return LLMResponse(
            content="",
            tool_calls=[ToolCallRequest(id=f"call_{self._calls}", name=action.tool, arguments=dict(action.args))],
            finish_reason="tool_calls",
            usage=self._usage(request.tokens, ""),
        )

    def _inject(self, queue: str, text: str) -> None:
        """A message arriving while the turn runs, routed as the gateway's
        consumer routes it: a steer or a system result into the running
        turn, a plain message to wait for its end."""
        pending = self.loop._pending_queues.get(KEY)
        if pending is None:
            return
        marker = USER_MARK.search(text)
        if queue == "queued":
            pending.deferred.put_nowait(InboundMessage(channel=CHANNEL, sender_id="user", chat_id=CHAT_ID, content=text))
        elif queue == "steer":
            pending.inject.put_nowait(InboundMessage(
                channel=CHANNEL, sender_id="user", chat_id=CHAT_ID, content=text, metadata={"steer": True},
            ))
        else:
            self._calls += 1
            pending.inject.put_nowait(InboundMessage(
                channel="system", sender_id="subagent", chat_id=KEY, content=text, session_key_override=KEY,
                metadata={"injected_event": "subagent_result", "subagent_task_id": f"inj-{self._calls}"},
            ))
        if marker:
            self.user_texts[marker.group(0)] = text
            self.record.injected.append(marker.group(0))

    # -- the session and its summary ------------------------------------------

    def _disk_session(self) -> Any:
        self.loop.sessions.invalidate(KEY)
        return self.loop.sessions.get_or_create(KEY)

    def _stored_summary(self) -> str | None:
        text, _ = get_session_summary(self.workspace, KEY)
        return text

    @staticmethod
    def _fixed_tokens(loop: AgentLoop) -> int:
        system = loop.context.build_system_prompt(None, channel=CHANNEL)
        return estimate_prompt_tokens([{"role": "system", "content": system}], loop.tools.get_definitions())

    def _write_agents_md(self, words: int, loop: AgentLoop | None = None) -> None:
        """An AGENTS.md that puts this workspace's fixed part at exactly
        ``reference_base() + words`` tokens, drawn from a generator of its
        own so its size does not shift the rest of the scenario's draws.
        Measured once written and cut again until it lands: where the text
        meets the prompt around it, its last token may join the next one
        (a period and the blank line after it are one token), and a token
        off would make a seed run differently in another directory."""
        loop = loop or self.loop
        target = reference_base() + words

        def write(tokens: int, tail: str) -> int:
            rng = random.Random(f"{self.scenario.seed}:agents:{words}")
            text = "# Project rules\n\n" + filler(rng, max(1, tokens)) + tail
            (self.workspace / "AGENTS.md").write_text(text, encoding="utf-8")
            return self._fixed_tokens(loop) - target

        tokens = target - self.base
        off = write(tokens, "")
        for _ in range(3):
            if off == 0:
                return
            tokens -= off
            off = write(tokens, "")
        best = (abs(off), tokens, "")
        for tail in (" ok", "."):
            for step in (0, -1, 1, -2, 2):
                off = write(tokens + step, tail)
                if off == 0:
                    return
                best = min(best, (abs(off), tokens + step, tail))
        write(best[1], best[2])

    def _seed(self) -> None:
        sc = self.scenario
        session = self.loop.sessions.get_or_create(KEY)
        if sc.seed_history:
            session.messages = [dict(m) for m in sc.seed_history]
            for message in session.messages:
                found = USER_MARK.search(text_of(message))
                if message.get("role") == "user" and found:
                    self.user_texts[found.group(0)] = text_of(message)
        self.loop.sessions.save(session)
        if sc.seed_summary_chars:
            blocks, i = [], 0
            while sum(len(b) + 5 for b in blocks) < sc.seed_summary_chars:
                blocks.append(f"- Span {i}: the user and the agent worked on item {i}, "
                              f"/srv/app{i}/config.yaml. " + filler(self.rng, 40))
                i += 1
            text = SUMMARY_BLOCK_SEP.join(blocks)
            if sc.seed_carried_paths:
                carried = CARRIED_PATHS + "; ".join(f"/srv/old{j}/settings.toml" for j in range(sc.seed_carried_paths))
                text = carried + SUMMARY_BLOCK_SEP + text
            write_session_summary(self.workspace, KEY, text[: sc.seed_summary_chars], last_active="2026-09-30")
        if sc.data_file_tokens or sc.small_files:
            (self.workspace / "notes").mkdir(exist_ok=True)
        if sc.data_file_tokens:
            (self.workspace / "notes" / "data.txt").write_text(filler(self.rng, sc.data_file_tokens), encoding="utf-8")
        for i in range(sc.small_files):
            (self.workspace / "notes" / f"file{i}.txt").write_text(filler(self.rng, 60), encoding="utf-8")

    # -- turns -----------------------------------------------------------------

    def _apply(self, event: tuple[str, Any]) -> None:
        name, value = event
        if name == "agents_md":
            self._write_agents_md(int(value))
            self._events.add("agents_md")
        elif name == "persona":
            session = self._disk_session()
            if value:
                session.metadata["persona"] = value
            else:
                session.metadata.pop("persona", None)
            self.loop.sessions.save(session)
            self._events.add("persona")

    async def _nightly(self) -> None:
        """The dream's session-summary pass over this session, as if it had
        gone idle: it summarizes the span no summary covers yet into the same
        store and moves its cursor past it, which compaction then skips."""

        def invoke(prompt: str, model: str | None = None) -> str:
            text = prompt[len(self._archive_prompt):].lstrip("\n") if prompt.startswith(self._archive_prompt) else prompt
            self.summarized.append((text, self.nightly_budget))
            self.record.requests.append(Request(
                kind="nightly", model=self.nightly_model, window=self.nightly_window,
                tokens=estimate_text_tokens(prompt), max_tokens=None, default_max=self.nightly_max_out, input=text,
            ))
            self._summaries += 1
            return f"- Span {self._summaries} (nightly): " + filler(self.rng, self.scenario.summary_tokens)

        # The dream gives the pass the input budget of the model it
        # summarizes with.
        summarize_session(
            self.workspace, self.loop.sessions._get_session_path(KEY), llm_invoke=invoke,
            idle_hours=0, min_new_messages=1, budget_tokens=self.nightly_budget,
        )

    async def _bus_turn(self, msg: InboundMessage) -> Any:
        """The gateway's path for a message from the bus: the turn task, its
        pending queues registered first, its reply published to the bus."""
        task = self.loop._start_turn_task(msg, KEY)
        try:
            await task
        except asyncio.CancelledError:
            self.record.crashed = "the turn task was cancelled"
        out = None
        while not self.loop.bus.outbound.empty():
            candidate = self.loop.bus.outbound.get_nowait()
            if candidate.content and not (candidate.metadata or {}).get("_progress"):
                out = candidate
        return out

    async def _turn(self, plan_index: int, plan: TurnPlan, start: Callable[[], Awaitable[Any]]) -> None:
        marker = USER_MARK.search(plan.text)
        record = TurnRecord(
            index=len(self.records), plan_index=plan_index, kind=plan.kind,
            marker=marker.group(0) if marker else None, events=self._events, stalled=plan.stall,
            planned_calls=len(plan.actions) + sum(len(a.inject) for a in plan.actions),
        )
        self._events = set()
        if plan.text.strip().lower() == "/new":
            record.events.add("new")
        if plan.text.strip().lower() == "/compact":
            record.events.add("compact")
        if record.marker and plan.kind in ("user", "cron", "workflow", "requeued"):
            self.user_texts[record.marker] = plan.text
        self.records.append(record)
        self.record = record
        self._actions = [copy.deepcopy(a) for a in plan.actions]
        self._reply_size = plan.reply_size
        self.stalled = plan.stall
        record.session_before = len(self._disk_session().messages)
        try:
            out = await start()
        except Exception as exc:  # noqa: BLE001 - a turn that raises is a finding, not a harness error
            record.crashed = record.crashed or f"{type(exc).__name__}: {exc}"
            out = None
        self.phase = "background"
        try:
            while self._background:
                await self._background.pop(0)
        finally:
            self.phase = "turn"
            self.stalled = False
        record.reply = out.content if out is not None else None
        record.stop_reason = (out.metadata or {}).get("_stop_reason") if out is not None else None
        session = self._disk_session()
        record.session = [dict(m) for m in session.messages]
        record.last_consolidated = session.last_consolidated
        if "new" in record.events:
            self.new_at = record.index + 1
        else:
            record.growth = sum(estimate_message_tokens(m) for m in record.session[record.session_before:])
        self.violations.extend(check_record(self, record))

    async def _play(self, plan_index: int, plan: TurnPlan) -> None:
        for event in plan.before:
            self._apply(event)
        if plan.kind == "user":
            msg = InboundMessage(channel=CHANNEL, sender_id="user", chat_id=CHAT_ID, content=plan.text)
            await self._turn(plan_index, plan, lambda: self._bus_turn(msg))
        elif plan.kind == "subagent":
            self._calls += 1
            msg = InboundMessage(
                channel="system", sender_id="subagent", chat_id=KEY, content=plan.text, session_key_override=KEY,
                metadata={"injected_event": "subagent_result", "subagent_task_id": f"bg-{self._calls}"},
            )
            self.system_results.append((plan.text, len(self.records)))
            await self._turn(plan_index, plan, lambda: self._bus_turn(msg))
        elif plan.kind == "workflow":
            msg = InboundMessage(
                channel="system", sender_id="workflow_background", chat_id=KEY, content=plan.text,
                session_key_override=KEY,
                metadata={"injected_event": "workflow_background_result", "workflow": "nightly"},
            )
            await self._turn(plan_index, plan, lambda: self._bus_turn(msg))
        elif plan.kind == "cron":
            await self._turn(plan_index, plan, lambda: self.loop.process_direct(
                plan.text, session_key=KEY, channel=CHANNEL, chat_id=CHAT_ID,
                model_preset=plan.model_preset, persona=plan.persona,
            ))
        elif plan.kind == "nightly":
            await self._turn(plan_index, plan, self._nightly)
        # What the turn left on its queues was re-published to the bus: the
        # gateway's consumer starts a turn for each, in order.
        while not self.loop.bus.inbound.empty():
            left = self.loop.bus.inbound.get_nowait()
            kind = "requeued_system" if left.channel == "system" else "requeued"
            await self._turn(plan_index, TurnPlan(kind=kind, text=left.content), lambda m=left: self._bus_turn(m))

    def _find_previous_summary(self, real: Callable[..., Any]) -> Callable[..., Any]:
        def find(*args: Any, **kwargs: Any) -> Any:
            if self.record is not None:
                if self.phase == "background":
                    self.record.background_scans += 1
                else:
                    self.record.scans += 1
            return real(*args, **kwargs)

        return find

    async def run(self) -> list[Violation]:
        find = self._find_previous_summary(session_summary_store.find_previous_session_summary)
        with harness_patches(), patch.object(session_summary_store, "find_previous_session_summary", find):
            self.loop = self._build()
            self.record = TurnRecord(
                index=-1, plan_index=-1, kind="seed", marker=None, events=set(), stalled=False, planned_calls=0,
            )
            self._seed()
            for plan_index, plan in enumerate(self.scenario.turns):
                await self._play(plan_index, plan)
            self.violations.extend(check_scenario(self))
            with contextlib.suppress(Exception):
                await self.loop.close_mcp()
        return self.violations


# ---------------------------------------------------------------------------
# Invariants
# ---------------------------------------------------------------------------


def _violation(driver: TurnDriver, record: TurnRecord, invariant: str, detail: str) -> Violation:
    return Violation(invariant, driver.scenario.seed, record.index, record.plan_index, detail)


def _roles(messages: list[dict[str, Any]]) -> str:
    roles = "".join({"user": "U", "assistant": "a", "tool": "t", "system": "s"}.get(m.get("role"), "?") for m in messages)
    return roles if len(roles) <= 40 else f"...{roles[-40:]} (last 40 of {len(roles)})"


def check_record(driver: TurnDriver, record: TurnRecord) -> list[Violation]:
    out: list[Violation] = []

    def fail(invariant: str, detail: str) -> None:
        out.append(_violation(driver, record, invariant, detail))

    session = record.session
    seeded = len(driver.scenario.seed_history) if driver.new_at == 0 else 0
    lc = record.last_consolidated

    # -- turn.no_crash
    if record.crashed or (record.reply or "").startswith("Sorry, I encountered an error."):
        fail("turn.no_crash", f"the turn raised: {record.crashed or record.reply}")

    # -- persistence.reply_saved_once / persistence.reply_order
    epoch = [(mark, idx) for mark, idx in driver.replies if idx >= driver.new_at]
    positions: list[int] = []
    for mark, idx in epoch:
        hits = [i for i, m in enumerate(session) if m.get("role") == "assistant" and mark in text_of(m)]
        if len(hits) != 1:
            fail("persistence.reply_saved_once",
                 f"reply {mark} (produced in turn {idx}) is saved {len(hits)} times; session roles {_roles(session)}")
        if hits:
            positions.append(hits[0])
    if positions != sorted(positions):
        fail("persistence.reply_order", f"replies are saved out of order: positions {positions}")
    if record.reply and REPLY_MARK.search(record.reply):
        delivered = REPLY_MARK.search(record.reply).group(0)
        if not any(delivered in text_of(m) for m in session if m.get("role") == "assistant"):
            fail("persistence.delivered_reply_saved", f"the reply the user got ({delivered}) is not in the session")

    # -- persistence.alternation / tool_pairs / ends_answered
    roles = [m.get("role") for m in session]
    for i in range(max(1, seeded), len(roles)):
        if roles[i] == roles[i - 1] == "user":
            fail("persistence.alternation",
                 f"two user messages without an answer between them at {i - 1},{i}: "
                 f"{text_of(session[i - 1])[:60]!r} / {text_of(session[i])[:60]!r}")
            break
    called: set[str] = set()
    for i, message in enumerate(session):
        for call in message.get("tool_calls") or []:
            called.add(call.get("id"))
        if message.get("role") == "tool" and message.get("tool_call_id") not in called:
            fail("persistence.tool_pairs", f"tool result {message.get('tool_call_id')} at {i} has no tool call before it")
            break
    if session and session[-1].get("role") == "user":
        fail("persistence.ends_answered", f"the session ends on a user message: {text_of(session[-1])[:80]!r}")

    # -- persistence.failure_notice
    failed = record.stop_reason in FAILED_STOP_REASONS
    if failed and record.marker:
        at = next((i for i, m in enumerate(session) if m.get("role") == "user" and record.marker in text_of(m)), None)
        if at is not None:
            # The turn's own messages run to the end of the session, past
            # any message it took in while it ran.
            notices = [m for m in session[at + 1:] if m.get("role") == "assistant"
                       and any(n.strip() and n.strip() in text_of(m) for n in FAILURE_NOTICES)]
            if not notices:
                fail("persistence.failure_notice",
                     f"turn failed ({record.stop_reason}) and left no assistant notice: {_roles(session[at:])}")

    # -- persistence.user_message_saved_once
    user_marks = Counter(mark for m in session if m.get("role") == "user" for mark in set(USER_MARK.findall(text_of(m))))
    for mark, count in user_marks.items():
        if count > 1:
            fail("persistence.user_message_saved_once", f"user message {mark} is saved {count} times")
    if record.marker and record.kind in ("user", "requeued", "cron", "workflow") and "new" not in record.events:
        if record.marker not in user_marks:
            fail("persistence.user_message_saved_once", f"the turn's own message {record.marker} is not in the session")

    # -- persistence.system_result_once
    for text, idx in driver.system_results:
        if idx < driver.new_at:
            continue
        count = sum(1 for m in session if text_of(m).strip() == text.strip())
        if count != 1:
            fail("persistence.system_result_once", f"the sub-agent result of turn {idx} is saved {count} times")

    # -- persistence.message_once_in_prompt
    for n, attempt in enumerate(record.attempts):
        if record.marker and record.kind in ("user", "requeued", "cron"):
            count = joined_text(attempt.messages).count(record.marker)
            if count != 1:
                fail("persistence.message_once_in_prompt",
                     f"attempt {n} ({attempt.stop_reason}) carries the turn's message {count} times")
    for request in record.requests:
        repeated = {mark: c for mark, c in request.user_marks.items() if c > 1}
        if repeated:
            fail("persistence.message_once_in_prompt", f"a request carries user messages more than once: {repeated}")
            break

    # -- content.no_loss / content.no_truncation
    for mark, text in driver.user_texts.items():
        # A sub-agent result the turn did not take in lands as an assistant
        # message of a turn of its own.
        positions = [i for i, m in enumerate(session) if mark in text_of(m)]
        if positions and positions[0] >= lc:
            continue
        received = [(given, budget) for given, budget in driver.summarized if mark in given]
        if any(text in given for given, _ in received):
            continue
        # A message larger than the call that covered it takes reaches that
        # call cut, alone, measured against that call's budget: compaction's
        # or the nightly pass's. One no call received at all is lost.
        tokens = estimate_text_tokens(text)
        if any(tokens + 64 > budget for _, budget in received):
            continue
        where = "archived" if positions else "gone from the session"
        fail("content.no_loss",
             f"user message {mark} ({tokens} tokens) is {where} but no summarizing call received it "
             + ("whole" if received else "at all"))
    for request in record.requests:
        if request.kind not in ("archive", "nightly"):
            continue
        lines = len(ARCHIVE_LINE.findall(request.input))
        if request.input.rstrip().endswith(TRUNCATED) and lines > 1:
            fail("content.no_truncation",
                 f"a summarizing call's input of {lines} messages was cut to {request.tokens} tokens")
        if request.input.startswith(NIGHTLY_OMITTED):
            fail("content.no_truncation",
                 f"the nightly summary pass left the earliest turns of its span out ({lines} messages kept)")
        # The placeholders of failed turns say nothing worth a summary block.
        placeholders = [n for n in FAILURE_NOTICES[:3] if n.strip() and n.strip() in request.input]
        if placeholders:
            fail("content.placeholders_left_out",
                 f"a {request.kind} summarizing call received {len(placeholders)} failure placeholder kinds")

    # -- budget.window / budget.precheck_accuracy
    for request in record.requests:
        if request.kind == "main" and request.window:
            asked = request.max_tokens if request.max_tokens is not None else request.default_max
            if request.tokens + asked > request.window:
                fail("budget.window",
                     f"a request of {request.tokens} tokens asked for {asked} more of a {request.window}-token window")
            if request.estimate is not None and abs(request.estimate - request.tokens) > ESTIMATE_TOLERANCE:
                fail("budget.precheck_accuracy",
                     f"the precheck sized a request of {request.tokens} tokens as {request.estimate}")
        elif request.kind in ("archive", "decisions", "learnings", "nightly") and request.window:
            if request.tokens + request.default_max > request.window:
                fail("budget.window",
                     f"a {request.kind} call of {request.tokens} tokens with {request.default_max} of output "
                     f"exceeds the {request.window}-token window")

    # -- budget.precheck_refusal: a run is not stopped for a request that fits
    for n, attempt in enumerate(record.attempts):
        if attempt.stop_reason == "mid_turn_precheck_overflow" and attempt.refusals:
            estimate, budget, sent = attempt.refusals[-1]
            if sent <= budget:
                fail("budget.precheck_refusal",
                     f"attempt {n} stopped on a request of {sent} tokens under its {budget}-token budget, "
                     f"estimated at {estimate}")

    # -- compaction.checks_per_turn
    # A turn checks once before its build and once after SAVE, and forces
    # one more compaction only for its single overflow retry: two that
    # compact and one forced at most, or compaction is looping.
    forced = [c for c in record.checks if c.force]
    unforced = [c for c in record.checks if not c.force and c.compacted]
    if len(forced) > 1 or len(unforced) > 2:
        fail("compaction.checks_per_turn",
             f"{len(unforced)} compactions and {len(forced)} forced ones in one turn")

    # -- compaction.calls_per_turn
    # A run makes the calls the scenario scripted, one more for each message
    # it took in, the answer after its last tool round and two retries after
    # blank answers; a turn runs at most twice (its one overflow retry).
    main = sum(1 for r in record.requests if r.kind == "main")
    extraction = sum(1 for r in record.requests if r.kind in ("decisions", "learnings"))
    archives = sum(1 for r in record.requests if r.kind == "archive")
    main_bound = 2 * (record.planned_calls + len(record.injected) + 4)
    if main > main_bound:
        fail("compaction.calls_per_turn", f"{main} model calls in one turn (bound {main_bound})")
    # The decision and learnings extraction read the span a compaction
    # archived, cut like it into the calls the summarizing model takes (a
    # head the nightly pass summarized already is read, not summarized).
    if extraction > 2 * (archives + 2):
        fail("compaction.calls_per_turn",
             f"{extraction} extraction calls for {archives} summarizing calls in one turn")

    # -- compaction.probe_scans
    # Finding the previous session's summary scans the whole summary store.
    # A fresh session's turn reads it in COMPACT and in each prompt it
    # builds; the compaction checks' probe builds measure the session's own
    # summary only, so the check after the turn scans nothing.
    allowed = 1 + len(record.attempts)
    if record.scans > allowed or record.background_scans:
        fail("compaction.probe_scans",
             f"{record.scans} scans of the summary store for the previous session's summary while the turn ran "
             f"(at most {allowed} for {len(record.attempts)} prompt builds) and {record.background_scans} in the "
             f"compaction check after it")

    # -- compaction.fitting_turn_answers
    # A turn whose own work (tool rounds, what it took in) outgrows the room
    # can fail later in its run; one that fits must at least reach the model.
    if record.attempts and record.kind in ("user", "requeued", "cron"):
        first, last = record.attempts[0], record.attempts[-1]
        budget = first.budget
        if budget and last.stop_reason == "mid_turn_precheck_overflow" and last.calls == 0:
            fixed = first.history_free(compressible=False)
            margin = FIT_MARGIN + int(FIT_MARGIN_SHARE * budget)
            if fixed + margin <= budget:
                compressible = last.history_free(compressible=True) - last.history_free(compressible=False)
                refused = (f"; it refused a request of {last.refusals[-1][2]} tokens (estimated "
                           f"{last.refusals[-1][0]})" if last.refusals else "")
                fail("compaction.fitting_turn_answers",
                     f"the turn failed on an overflow before its first model call though its system prompt, "
                     f"tools and message take {fixed} of a {budget}-token budget; attempts "
                     f"{[a.stop_reason for a in record.attempts]}, the last one replaying "
                     f"{len(last.messages) - 2} messages, its summary and decision log taking {compressible} "
                     f"tokens{refused}")

    # -- content.summary_keeps_carried_paths
    for n, attempt in enumerate(record.attempts):
        stored = attempt.summary or ""
        if not stored.startswith(CARRIED_PATHS):
            continue
        carried = stored.split(SUMMARY_BLOCK_SEP, 1)[0]
        _stable, carried_summary = _system_parts(attempt)
        if carried_summary and PREVIOUS_SESSION not in carried_summary and carried not in carried_summary:
            fail("content.summary_keeps_carried_paths",
                 f"attempt {n} carries {estimate_text_tokens(carried_summary)} tokens of the summary without "
                 f"the paths of its evicted blocks ({len(carried)} chars)")

    # -- turn.system_message_model
    if record.kind in ("subagent", "workflow") and record.attempts:
        own = [r for r in driver.records[driver.new_at:record.index]
               if r.kind in ("user", "requeued", "cron") and r.attempts]
        if own and own[-1].attempts[0].model != record.attempts[0].model:
            fail("turn.system_message_model",
                 f"the {record.kind} result ran on {record.attempts[0].model}, the session's latest turn on "
                 f"{own[-1].attempts[0].model}")

    # -- prompt.stable_prefix / prompt.probe_matches_turn
    out.extend(_check_prompt_stability(driver, record))
    if record.attempts and record.probe_system is not None:
        first = record.attempts[0]
        system = text_of(first.messages[0]) if first.messages else ""
        if (
            first.summary == record.probe_summary
            and first.message_tokens < MESSAGE_ALLOWANCE
            and PREVIOUS_SESSION not in system
            and system != record.probe_system
        ):
            where = len(os.path.commonprefix([system, record.probe_system]))
            fail("prompt.probe_matches_turn",
                 f"the compaction check measured another system prompt than the turn sent "
                 f"({estimate_text_tokens(record.probe_system)} vs {estimate_text_tokens(system)} tokens), from char "
                 f"{where}: {record.probe_system[where:where + 50]!r} vs {system[where:where + 50]!r}")
    return out


def _system_parts(attempt: Attempt) -> tuple[str, str]:
    system = text_of(attempt.messages[0]) if attempt.messages else ""
    cut = system.find(SUMMARY_BLOCK)
    return (system, "") if cut < 0 else (system[:cut], system[cut:])


def _check_prompt_stability(driver: TurnDriver, record: TurnRecord) -> list[Violation]:
    """The system prompt (summary included) is the head of every provider's
    prompt cache: it may change only when one of its inputs did."""
    if not record.attempts:
        return []
    previous = next((r for r in reversed(driver.records[:record.index]) if r.attempts), None)
    if previous is None:
        return []
    events = set()
    for r in driver.records[previous.index + 1:record.index + 1]:
        events |= r.events
    a, b = previous.attempts[0], record.attempts[0]
    stable_a, summary_a = _system_parts(a)
    stable_b, summary_b = _system_parts(b)
    out: list[Violation] = []
    model_changed = a.model != b.model or a.window != b.window
    surface_changed = a.surface != b.surface or a.surface is None
    if stable_a != stable_b and not (events & {"agents_md", "persona", "new"} or model_changed or surface_changed):
        where = len(os.path.commonprefix([stable_a, stable_b]))
        out.append(_violation(driver, record, "prompt.stable_prefix",
                              f"the system prompt's fixed tiers changed with no input changed, at char {where}: "
                              f"{stable_a[where:where + 60]!r} -> {stable_b[where:where + 60]!r}"))
    # The summary's room is what the fixed tiers leave of the budget: a
    # change there (reported above when nothing explains it) moves the cut.
    explained = (
        a.summary != b.summary
        or model_changed
        or stable_a != stable_b
        or events & {"new", "compact", "persona", "agents_md"}
        or (PREVIOUS_SESSION in summary_a) != (PREVIOUS_SESSION in summary_b)
    )
    record.prompt_changes = 0 if explained else previous.prompt_changes
    if summary_a != summary_b and not explained:
        large = max(a.message_tokens, b.message_tokens) >= MESSAGE_ALLOWANCE
        if not large:
            # Once per stored summary the history may outgrow the room the
            # whole summary needs, and the cut that follows holds.
            record.prompt_changes += 1
            if record.prompt_changes > 1:
                out.append(_violation(
                    driver, record, "prompt.stable_prefix",
                    f"the summary the system prompt carries changed again ({estimate_text_tokens(summary_a)} -> "
                    f"{estimate_text_tokens(summary_b)} tokens) with the stored summary unchanged and messages of "
                    f"{a.message_tokens} and {b.message_tokens} tokens",
                ))
    return out


def check_scenario(driver: TurnDriver) -> list[Violation]:
    """What can only be judged over many turns: how often a session whose
    turns keep fitting compacts when there is room."""
    out: list[Violation] = []
    segment: list[TurnRecord] = []

    def close() -> None:
        if len(segment) < 6:
            return
        rooms, growths = [], []
        for r in segment:
            a = r.attempts[0]
            rooms.append(a.budget - a.history_free(compressible=True))
            growths.append(r.growth)
        room, growth = min(rooms), max(growths)
        compacting = [r.index for r in segment if any(c.compacted and not c.force for c in r.checks)]
        # With room for four turns of growth, a compaction leaves the summary
        # and the last turn, so at least two turns fit before the next one is
        # needed: at most one turn in two compacts.
        if growth > 0 and room >= 4 * growth and len(compacting) > math.ceil(len(segment) / 2):
            out.append(_violation(
                driver, segment[-1], "compaction.frequency",
                f"{len(compacting)} of {len(segment)} turns {segment[0].index}..{segment[-1].index} compacted "
                f"with room for {room // growth} turns of growth ({room} tokens, {growth} a turn): {compacting}",
            ))

    previous_model = None
    for r in driver.records:
        steady = (
            r.kind in ("user", "requeued") and r.attempts and not r.events and not r.stalled
            and r.stop_reason not in FAILED_STOP_REASONS and not r.injected
            and r.attempts[0].model == previous_model
            and r.attempts[0].budget
            and r.attempts[0].message_tokens <= 0.1 * r.attempts[0].budget
        )
        if r.attempts:
            previous_model = r.attempts[0].model
        if steady:
            segment.append(r)
        else:
            close()
            segment = [r] if (r.kind in ("user", "requeued") and r.attempts and not r.events) else []
    close()
    return out


def scenario_health(driver: TurnDriver) -> list[str]:
    """What the scenario was built to exercise and did not: a scenario that
    never compacts, or whose turns all fail, would keep every invariant
    without testing it. Empty when it did what its profile is for."""
    records = driver.records
    turns = [r for r in records if r.attempts and r.kind in ("user", "requeued", "cron")]
    answered = [r for r in turns if r.stop_reason == "completed"]
    compactions = sum(1 for r in records for c in r.checks if c.compacted)
    archives = sum(1 for r in records for q in r.requests if q.kind == "archive")
    problems = []
    if turns and len(answered) * 2 < len(turns):
        problems.append(f"only {len(answered)} of {len(turns)} turns were answered")
    profile = driver.scenario.profile
    if profile in ("ceiling", "fixed_band", "big_model") and compactions < 1:
        problems.append("no compaction ran")
    if profile == "summary_growth" and compactions < 3:
        problems.append(f"{compactions} compactions, the summary never grew")
    notices = any(
        m.get("role") == "assistant" and any(n.strip() and n.strip() in text_of(m) for n in FAILURE_NOTICES)
        for r in records for m in r.session
    )
    if profile == "retry_summary" and (archives < 1 or not notices):
        problems.append("no failed exchange was saved and summarized")
    if profile == "task_state" and not any(a.calls >= 3 for r in records for a in r.attempts):
        problems.append("no turn ran several tool rounds")
    if profile == "chatty_stalled":
        stalled = next((r for r in records if r.stalled and r.attempts), None)
        if stalled is None or len(stalled.attempts[0].messages) < 100:
            problems.append("the stalled turn did not replay a long history")
    if profile == "cache" and not any(_system_parts(a)[1] for r in records for a in r.attempts):
        problems.append("no prompt carried the summary")
    if profile == "small_dream":
        cut = {
            mark for r in records for q in r.requests
            if q.kind == "nightly" and q.input.rstrip().endswith(TRUNCATED) for mark in USER_MARK.findall(q.input)
        }
        last = records[-1] if records else None
        archived = last.session[:last.last_consolidated] if last else []
        if not cut:
            problems.append("the nightly pass never cut a message larger than its model takes")
        elif not any(mark in text_of(m) for mark in cut for m in archived):
            problems.append("no compaction archived a message the nightly pass cut")
    return problems


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


_BASE_FIXED: list[int] = []


def app_config() -> Config:
    """The configuration a scenario's loop runs with: the defaults, without
    the background services a loop starts on its own threads (the memory
    file watcher and health check, the model and MCP catalog refreshes,
    which fetch over the network): left running they outlive the test, and
    a catalog row that changes mid-scenario would change its windows."""
    config = Config()
    config.memory.file_watcher.enabled = False
    config.memory.health_check.enabled = False
    config.catalog_refresh.enabled = False
    config.mcp_catalog_refresh.enabled = False
    return config


def base_fixed_tokens() -> int:
    """Tokens of the system prompt and tool definitions of a scenario's loop
    with no AGENTS.md: what every prompt carries before anything of the
    session (measured in a temporary workspace)."""
    if not _BASE_FIXED:
        with tempfile.TemporaryDirectory() as d, harness_patches():
            provider = ScriptedProvider(None, LOOP_MODEL, 8192)  # type: ignore[arg-type]
            loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=Path(d), model=LOOP_MODEL,
                             app_config=app_config())
            system = loop.context.build_system_prompt(None, channel=CHANNEL)
            _BASE_FIXED.append(estimate_prompt_tokens([{"role": "system", "content": system}], loop.tools.get_definitions()))
    return _BASE_FIXED[0]


def reference_base() -> int:
    """The fixed part a scenario plans with: the base rounded up to the next
    thousand, and a thousand more. Every workspace's AGENTS.md makes up the
    difference from its own base, so a seed's prompts are the same size in
    any checkout and temporary directory, and the plan holds while the
    system prompt and the tool definitions change by less than that."""
    return math.ceil(base_fixed_tokens() / 1_000) * 1_000 + 1_000


class _Plans:
    """Turn plans for one scenario, every user text marked uniquely."""

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.n = 0

    def text(self, tokens: int, prefix: str = "") -> str:
        self.n += 1
        body = filler(self.rng, tokens)
        return f"[[u{self.n}]] {prefix}{body}"

    def short(self, words: str) -> str:
        self.n += 1
        return f"[[u{self.n}]] {words}"

    def user(self, tokens: int, reply: int = 200, actions: list[Action] | None = None, **kw: Any) -> TurnPlan:
        return TurnPlan(kind="user", text=self.text(tokens), reply_size=reply, actions=actions or [], **kw)


def _budget(window: int, max_out: int, block_limit: int | None = None) -> int:
    return input_budget_tokens(window, max_out, block_limit) or 0


def _small_loop(rng: random.Random, room_lo: int, room_hi: int) -> tuple[ModelSpec, int]:
    """A small window (32K to 64K) and the AGENTS.md that leaves the input
    budget *room_lo* to *room_hi* tokens beside the fixed part."""
    base = reference_base()
    for _ in range(200):
        window = rng.randint(32_768, 64_000)
        max_out = rng.choice((2_048, 4_096, 8_192))
        room = rng.randint(room_lo, room_hi)
        words = _budget(window, max_out) - base - room
        if words >= 0:
            return ModelSpec("default", LOOP_MODEL, window, max_out), words
    return ModelSpec("default", LOOP_MODEL, 64_000, 4_096), 0


def _tool(rng: random.Random) -> Action:
    name = rng.choice(("list_dir", "todo_write", "note_decision", "read_file"))
    if name == "list_dir":
        return Action("tool", tool="list_dir", args={"path": "."})
    if name == "read_file":
        return Action("tool", tool="read_file", args={"path": "notes/data.txt"})
    if name == "note_decision":
        return Action("tool", tool="note_decision", args={"text": "We chose this: " + filler(rng, rng.randint(30, 90))})
    return Action("tool", tool="todo_write", args={"todos": [
        {"content": filler(rng, rng.randint(8, 25)), "status": rng.choice(("pending", "in_progress", "completed")),
         "activeForm": "Working on item " + str(i)}
        for i in range(rng.randint(3, 10))
    ]})


def profile_ceiling(rng: random.Random) -> Scenario:
    """A small window whose compaction waits for the ceiling: either the
    trigger is the ceiling (a window under four times its output reservation
    and buffers), or AGENTS.md puts the fixed part over the trigger. The
    turn's message can push a prompt BUILD found under the ceiling over the
    budget, and the turn retries after a forced compaction."""
    plans = _Plans(rng)
    base = reference_base()
    if rng.random() < 0.5:
        # Under 40,960 tokens with an 8,192-token output ceiling; at least
        # 4,000 tokens of room beside the fixed part.
        window = rng.randint(min(base + 4_000 + 8_192 + 1_024, 40_900), 40_900)
        loop_model = ModelSpec("default", LOOP_MODEL, window, 8_192)
        words = max(0, min(rng.randint(0, 1_500), _budget(window, 8_192) - base - 4_000))
    else:
        window = rng.randint(48_000, 64_000)
        max_out = rng.choice((4_096, 8_192))
        loop_model = ModelSpec("default", LOOP_MODEL, window, max_out)
        trigger = min(int(window * 0.75), window - max_out - 2_048)
        words = max(0, trigger - base + rng.randint(300, 2_500))
        words = min(words, _budget(window, max_out) - base - 3_000)
    turns = []
    error_at = rng.randint(2, 6)
    for i in range(rng.randint(10, 13)):
        if i == error_at:
            turns.append(plans.user(rng.randint(300, 1_200), actions=[Action("error")]))
            turns.append(TurnPlan(kind="user", text=plans.short("try again please"), reply_size=rng.randint(100, 500)))
            continue
        actions = [Action("tool", tool="list_dir", args={"path": "."})] if rng.random() < 0.15 else []
        turns.append(plans.user(rng.randint(300, 1_800), reply=rng.randint(100, 600), actions=actions))
    return Scenario(seed=0, profile="ceiling", loop_model=loop_model, agents_words=words,
                    summary_tokens=rng.randint(150, 500), turns=turns)


def profile_fixed_band(rng: random.Random) -> Scenario:
    """A fixed prompt (system prompt, tools, a long AGENTS.md) just under the
    trigger, on a 1M window under a cap or on a small window under its
    floor: a compaction leaves less than a turn of room under the trigger."""
    plans = _Plans(rng)
    base = reference_base()
    ratio, cap = 0.5, 256_000
    variant = rng.randrange(3)
    if variant == 0:
        # Under the absolute cap.
        cap = rng.choice((64_000, 72_000, 80_000))
        loop_model = ModelSpec("default", LOOP_MODEL, 1_000_000, rng.choice((8_192, 32_000)))
        trigger = cap
    elif variant == 1:
        # Under a low ratio of a large window.
        ratio = rng.choice((0.064, 0.08))
        loop_model = ModelSpec("default", LOOP_MODEL, 1_000_000, rng.choice((8_192, 32_000)))
        trigger = int(1_000_000 * ratio)
    else:
        # Under a small window's floor.
        window = rng.choice((56_000, 64_000, 72_000))
        loop_model = ModelSpec("default", LOOP_MODEL, window, 8_192)
        trigger = min(int(window * 0.75), window - 8_192 - 2_048)
    # A turn adds about a thousand tokens. A compaction keeps the last turn
    # and a short summary: with the fixed part one to two and a half turns
    # under the trigger, it leaves less than a turn of room there.
    words = max(0, trigger - base - rng.randint(1_400, 2_600))
    turns = [plans.user(rng.randint(600, 900), reply=rng.randint(150, 250)) for _ in range(12)]
    return Scenario(seed=0, profile="fixed_band", loop_model=loop_model, ratio=ratio, cap=cap,
                    agents_words=words, summary_tokens=rng.randint(30, 80), turns=turns)


def profile_summary_growth(rng: random.Random) -> Scenario:
    """A small window and large summaries: the summary reaches its cap in a
    few compactions, and with the decision log it would leave a turn no
    room unless both give way to the turn's own message."""
    plans = _Plans(rng)
    # Room for a message and a summary at its cap (16,000 characters, about
    # 3,000 tokens of this text), not for both and the decision log at full.
    loop_model, words = _small_loop(rng, 3_500, 6_000)
    files = 12
    turns = []
    for _ in range(rng.randint(14, 18)):
        # A file read leaves its path in the span's summary; once the store
        # evicts that block, the path rides in its carried head block.
        actions = ([Action("tool", tool="read_file", args={"path": f"notes/file{rng.randrange(files)}.txt"})]
                   if rng.random() < 0.5 else [])
        turns.append(plans.user(rng.randint(900, 1_800), reply=rng.randint(100, 300), actions=actions))
    return Scenario(seed=0, profile="summary_growth", loop_model=loop_model, agents_words=words,
                    summary_tokens=rng.randint(600, 1_200), decisions_per_compaction=rng.randint(2, 4),
                    small_files=files, turns=turns)


def profile_retry_summary(rng: random.Random) -> Scenario:
    """A model call fails and the user answers "try again" (once with
    messages queued behind the failure); the span is then summarized: by the
    nightly pass over the idle session, by hand with /compact, and by the
    record /new files when the session is started over. Some sessions carry
    long messages, a span longer than one summarizing call takes."""
    plans = _Plans(rng)
    window = rng.choice((128_000, 160_000, 200_000))
    loop_model = ModelSpec("default", LOOP_MODEL, window, rng.choice((8_192, 16_384, 32_000)))
    size = (2_500, 4_000) if rng.random() < 0.4 else (40, 200)
    turns = [plans.user(rng.randint(*size), reply=rng.randint(60, 200)) for _ in range(rng.randint(1, 3))]
    fail = Action(rng.choice(("error", "overflow")))
    if rng.random() < 0.5:
        fail.inject = [("queued", plans.text(rng.randint(20, 80))), ("queued", plans.text(rng.randint(20, 80)))]
    turns.append(TurnPlan(kind="user", text=plans.text(rng.randint(40, 200), "Set up port 8443 and vault path. "),
                          actions=[fail]))
    turns.append(TurnPlan(kind="user", text=plans.short("try again"), reply_size=rng.randint(60, 200)))
    turns += [plans.user(rng.randint(*size), reply=rng.randint(60, 200)) for _ in range(rng.randint(1, 3))]
    if rng.random() < 0.6:
        turns.append(TurnPlan(kind="nightly"))
        turns.append(plans.user(rng.randint(40, 200)))
    turns.append(TurnPlan(kind="user", text="/compact"))
    turns.append(plans.user(rng.randint(40, 200)))
    turns.append(TurnPlan(kind="user", text="/new"))
    turns.append(plans.user(rng.randint(40, 200)))
    return Scenario(seed=0, profile="retry_summary", loop_model=loop_model,
                    agents_words=rng.randint(0, 2_000), turns=turns)


def profile_task_state(rng: random.Random) -> Scenario:
    """Turns of several tool rounds that change the task state mid-turn:
    every later request appends it, and the precheck must count it once."""
    plans = _Plans(rng)
    if rng.random() < 0.6:
        loop_model, words = _small_loop(rng, 6_000, 14_000)
    else:
        loop_model, words = ModelSpec("default", LOOP_MODEL, rng.choice((128_000, 200_000)), 8_192), rng.randint(0, 3_000)
    turns = []
    for _ in range(rng.randint(7, 10)):
        actions = [_tool(rng) for _ in range(rng.randint(1, 4))]
        if rng.random() < 0.3:
            actions.insert(0, Action("tool", tool="long_task", args={"goal": filler(rng, rng.randint(20, 60))}))
        if rng.random() < 0.2:
            actions.append(Action("tool", tool="complete_goal", args={"recap": filler(rng, 20)}))
        turns.append(plans.user(rng.randint(100, 900), reply=rng.randint(80, 400), actions=actions))
    return Scenario(seed=0, profile="task_state", loop_model=loop_model, agents_words=words,
                    data_file_tokens=rng.randint(1_000, 6_000), turns=turns)


def profile_chatty_stalled(rng: random.Random) -> Scenario:
    """A session of hundreds of short messages on a small window, and a turn
    whose compaction cannot run: the history the turn replays has to be cut
    to what the budget leaves, message by message."""
    plans = _Plans(rng)
    loop_model, words = _small_loop(rng, 2_500, 5_000)
    history = []
    for i in range(rng.randint(300, 560)):
        history += [
            {"role": "user", "content": plans.short(filler(rng, rng.randint(4, 14)).rstrip(".")),
             "timestamp": "2026-09-30T10:00:00"},
            {"role": "assistant", "content": f"answer {i}: " + filler(rng, rng.randint(4, 12)).rstrip("."),
             "timestamp": "2026-09-30T10:00:01"},
        ]
    turns = [plans.user(rng.randint(30, 200), stall=True)]
    turns += [plans.user(rng.randint(30, 400)) for _ in range(rng.randint(3, 5))]
    return Scenario(seed=0, profile="chatty_stalled", loop_model=loop_model, agents_words=words,
                    max_messages=rng.choice((480, 480, 800)), seed_history=history, turns=turns)


def profile_cache(rng: random.Random) -> Scenario:
    """A long AGENTS.md on a 44K to 64K window and a stored summary at its
    cap: the summary the system prompt carries is cut, and messages of very
    different sizes must not move the cut from turn to turn."""
    plans = _Plans(rng)
    window = rng.choice((64_000, 48_000, 44_000))
    loop_model = ModelSpec("default", LOOP_MODEL, window, 8_192)
    # Five to eight thousand tokens of room beside the fixed part: the
    # summary's share of it (a quarter of what the turn's message leaves)
    # holds the carried paths and some blocks, never the whole summary.
    # With less, not even the carried paths fit and no prompt carries any.
    words = max(0, _budget(window, 8_192) - reference_base() - rng.randint(5_000, 8_000))
    sizes = [rng.choice((20, 30, 40, 60, 100, 250, 400, 600, 900, 1_200, 1_500)) for _ in range(rng.randint(12, 16))]
    turns = [plans.user(size, reply=rng.randint(20, 60)) for size in sizes]
    return Scenario(seed=0, profile="cache", loop_model=loop_model, agents_words=words,
                    seed_summary_chars=15_900, seed_carried_paths=rng.randint(0, 30),
                    summary_tokens=rng.randint(150, 400), turns=turns)


def profile_big_model(rng: random.Random) -> Scenario:
    """A loop on a 32K model whose session runs on a 1M persona model: what
    compaction archives is sized by the persona's window, but the summarizer
    runs on the loop's model and must take it in as many calls as needed."""
    plans = _Plans(rng)
    loop_model = ModelSpec("default", LOOP_MODEL, 32_768, 4_096)
    big = ModelSpec("big", "big-model", 1_000_000, 8_192, cap=rng.choice((64_000, 64_000, 80_000)))
    turns = [plans.user(rng.randint(4_000, 8_000), reply=rng.randint(100, 300)) for _ in range(rng.randint(9, 11))]
    turns[0].before.append(("persona", "wide"))
    return Scenario(seed=0, profile="big_model", loop_model=loop_model, presets=[big],
                    personas={"wide": (60, "big")}, summary_tokens=rng.randint(150, 400), turns=turns)


def profile_mixed(rng: random.Random) -> Scenario:
    """Every kind of event, drawn at random."""
    plans = _Plans(rng)
    pick = rng.random()
    if pick < 0.45:
        loop_model, words = _small_loop(rng, 4_000, 14_000)
    elif pick < 0.85:
        loop_model = ModelSpec("default", LOOP_MODEL, rng.choice((128_000, 160_000, 200_000)),
                               rng.choice((8_192, 16_384, 32_000)))
        words = rng.randint(0, 6_000)
    else:
        loop_model = ModelSpec("default", LOOP_MODEL, 1_000_000, rng.choice((8_192, 32_000)))
        words = rng.randint(0, 20_000)
    cap = rng.choice((64_000, 100_000, 256_000)) if loop_model.window >= 500_000 else rng.choice((256_000, 64_000, None))
    if loop_model.window >= 500_000 and cap == 256_000:
        cap = 100_000
    persona_window = rng.choice((48_000, 64_000, 128_000, 200_000))
    persona_max = rng.choice((4_096, 8_192))
    if _budget(persona_window, persona_max) < reference_base() + words + 4_000:
        persona_window = 200_000
    # The other model's preset sets its own ratio and cap, or inherits them.
    other = ModelSpec("other", "other-model", persona_window, persona_max,
                      ratio=rng.choice((None, None, 0.3, 0.6)), cap=rng.choice((None, None, 0, 64_000, 128_000)))
    budget = _budget(loop_model.window, loop_model.max_out)
    fixed = reference_base() + words
    persona_on = False
    turns: list[TurnPlan] = []
    retry_next = False
    for _ in range(rng.randint(10, 14)):
        roll = rng.random()
        if retry_next:
            turns.append(TurnPlan(kind="user", text=plans.short("try again"), reply_size=rng.randint(50, 300)))
            retry_next = False
            continue
        if roll < 0.06:
            turns.append(TurnPlan(kind="subagent", text=f"[sub-agent result] {filler(rng, rng.randint(40, 400))}"))
            continue
        if roll < 0.12:
            turns.append(TurnPlan(kind="workflow", text=plans.text(rng.randint(40, 400), "[Background workflow finished] ")))
            continue
        if roll < 0.18:
            turns.append(TurnPlan(kind="cron", text=plans.text(rng.randint(40, 600)), model_preset="other",
                                  reply_size=rng.randint(50, 300)))
            if rng.random() < 0.5:
                # The result of a sub-agent the job's run started lands on
                # its session after the run: it runs on the job's model.
                turns.append(TurnPlan(kind="subagent", text=f"[sub-agent result] {filler(rng, rng.randint(40, 400))}"))
            continue
        if roll < 0.22:
            turns.append(TurnPlan(kind="user", text="/compact"))
            continue
        if roll < 0.25:
            turns.append(TurnPlan(kind="user", text="/new"))
            continue
        if roll < 0.28:
            turns.append(TurnPlan(kind="nightly"))
            continue
        before: list[tuple[str, Any]] = []
        if rng.random() < 0.06:
            before.append(("agents_md", max(0, int(words * rng.uniform(0.6, 1.2)))))
        if rng.random() < 0.08:
            persona_on = not persona_on
            before.append(("persona", "alt" if persona_on else None))
        if rng.random() < 0.05:
            size = max(20, budget - fixed - rng.randint(-600, 1_500))
        else:
            size = int(rng.choice((20, 60, 150, 400, 900, 1_600, 3_000)) * rng.uniform(0.7, 1.3))
        actions: list[Action] = []
        if rng.random() < 0.25:
            actions += [_tool(rng) for _ in range(rng.randint(1, 3))]
        fail_roll = rng.random()
        if fail_roll < 0.06:
            actions.append(Action("error"))
            retry_next = rng.random() < 0.7
        elif fail_roll < 0.09:
            actions.append(Action("overflow"))
            retry_next = rng.random() < 0.7
        elif fail_roll < 0.13:
            actions += [Action("empty") for _ in range(rng.randint(1, 2))]
        if rng.random() < 0.15:
            queue = rng.choice(("queued", "steer", "subagent"))
            injected = (plans.text(rng.randint(20, 200)) if queue != "subagent"
                        else plans.text(rng.randint(20, 200), "[sub-agent result] "))
            target = actions[0] if actions else Action("answer", size=rng.randint(50, 300))
            target.inject.append((queue, injected))
            if not actions:
                actions.append(target)
        turns.append(plans.user(size, reply=rng.randint(30, 500), actions=actions, before=before,
                                stall=rng.random() < 0.05))
    return Scenario(
        seed=0, profile="mixed", loop_model=loop_model, ratio=rng.choice((0.3, 0.5, 0.5, 0.7, 0.9)), cap=cap,
        agents_words=words,
        block_limit=rng.choice((None, None, None, budget - rng.randint(0, 4_000))) if loop_model.window >= 128_000 else None,
        presets=[other], personas={"alt": (rng.randint(50, 1_500), "other" if rng.random() < 0.7 else None)},
        summary_tokens=rng.randint(100, 900), decisions_per_compaction=rng.randint(0, 3),
        data_file_tokens=rng.randint(500, 8_000), turns=turns,
    )


def profile_small_dream(rng: random.Random) -> Scenario:
    """The dream summarizes with a memory model of a smaller window than the
    loop's. The nightly pass covers messages too large for that model but
    not for compaction: it sizes its calls by that model, so each such
    message reaches its call cut, alone, and is summarized; its cursor moves
    past it, and compaction, which skips what the cursor covers, never
    receives it whole."""
    plans = _Plans(rng)
    loop_model = ModelSpec("default", LOOP_MODEL, rng.choice((200_000, 256_000)), rng.choice((8_192, 16_384)))
    dream = ModelSpec("memory", "dream-model", rng.choice((16_384, 24_000, 32_768)), rng.choice((2_048, 4_096)))
    nightly = dream.window - dream.max_out - Consolidator._SAFETY_BUFFER
    sizes = [rng.randint(40, 300)]
    for _ in range(rng.randint(1, 2)):
        sizes += [int(nightly * rng.uniform(1.15, 1.6)), rng.randint(40, 300)]
    turns = [plans.user(size, reply=rng.randint(60, 200)) for size in sizes]
    turns.append(TurnPlan(kind="nightly"))
    # The session then grows past its trigger, and compaction archives the
    # span, skipping the head the nightly pass covered: the pass is the only
    # call that ever covers those messages. The ratio leaves them in the
    # session until then.
    ratio = 0.6
    while sum(sizes) < ratio * loop_model.window + 20_000:
        sizes.append(rng.randint(15_000, 30_000))
        turns.append(plans.user(sizes[-1], reply=rng.randint(60, 200)))
    turns.append(plans.user(rng.randint(40, 200)))
    return Scenario(seed=0, profile="small_dream", loop_model=loop_model, dream_model=dream, ratio=ratio,
                    agents_words=rng.randint(0, 2_000), turns=turns)


PROFILES: tuple[Callable[[random.Random], Scenario], ...] = (
    profile_ceiling,
    profile_fixed_band,
    profile_summary_growth,
    profile_retry_summary,
    profile_task_state,
    profile_chatty_stalled,
    profile_cache,
    profile_big_model,
    profile_mixed,
    profile_small_dream,
)


def generate(seed: int) -> Scenario:
    """The scenario of *seed*: its profile is ``PROFILES[seed % len]``, every
    number in it drawn from the seed."""
    rng = random.Random(seed)
    build = PROFILES[seed % len(PROFILES)]
    scenario = build(rng)
    scenario.seed = seed
    return scenario


async def run_seed(seed: int, workspace: Path) -> tuple[Scenario, TurnDriver, list[Violation]]:
    scenario = generate(seed)
    driver = TurnDriver(scenario, workspace)
    violations = await driver.run()
    return scenario, driver, violations


def main(argv: list[str]) -> int:
    seeds = [int(a) for a in argv] or [0]
    worst = 0
    for seed in seeds:
        with tempfile.TemporaryDirectory() as d:
            scenario, driver, violations = asyncio.run(run_seed(seed, Path(d)))
        print(scenario.describe())
        for record in driver.records:
            print(f"  turn {record.index} ({record.kind}) stop={record.stop_reason} attempts="
                  f"{[a.stop_reason for a in record.attempts]} checks={[(c.phase, c.force, c.compacted) for c in record.checks]}")
        for violation in violations:
            print("  VIOLATION", violation)
        worst = max(worst, len(violations))
    return 1 if worst else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
