"""Automatic memory search per user turn: one warm memory_search with the
message as the query, fenced into the API copy of the user message."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from durin.agent.approval import AUTONOMOUS_SESSION_PREFIXES
from durin.agent.context import ContextBuilder, build_memory_context_block
from durin.agent.loop import AgentLoop
from durin.bus.events import InboundMessage
from durin.bus.queue import MessageBus
from durin.config.schema import MemoryPrefetchConfig
from durin.memory.fts_index import FTSIndex, fts_index_path
from durin.providers.base import LLMResponse, ToolCallRequest

_RENDERED = (
    "=== CANONICAL: person:ana (complete) ===\nAna\nAna runs the bakery on Main St.\n=== END CANONICAL ==="
)


class _FakeSearch:
    name = "memory_search"

    def __init__(self, total: int = 1, delay: float = 0.0, rendered: str = _RENDERED) -> None:
        self.calls: list[dict] = []
        self.total = total
        self.delay = delay
        self.rendered = rendered

    async def execute(self, **kwargs):
        self.calls.append(dict(kwargs))
        if self.delay:
            await asyncio.sleep(self.delay)
        return {"total": self.total, "strategy": "hybrid", "ranking": "rrf",
                "sectioned_rendered": self.rendered if self.total else ""}


class _RaisingSearch:
    name = "memory_search"

    async def execute(self, **kwargs):
        raise RuntimeError("search backend unavailable")


class _ErrorDictSearch:
    """The tool's own refusal shape: a dict with an ``error`` key."""

    name = "memory_search"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def execute(self, **kwargs):
        self.calls.append(dict(kwargs))
        return {"error": "query is required"}


class _Rec:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def log(self, event_type, data=None):
        self.events.append((event_type, dict(data or {})))


def _make_loop(
    tmp_path: Path, monkeypatch, *, fake: _FakeSearch | None = None, with_index: bool = True
) -> tuple[AgentLoop, list[list[dict]], _Rec]:
    if with_index:
        with FTSIndex.open(tmp_path):      # a real index file → the prefetch gate is open
            pass
    rec = _Rec()
    monkeypatch.setattr("durin.telemetry.logger.get_session_logger", lambda key, base_dir=None: rec)
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, model="test-model")
    if not with_index:
        # AgentLoop's own default tool registration opens the FTS index as a
        # side effect, regardless of the pre-open above; remove it so there
        # is genuinely no index on disk for the no_index gate to find.
        fts_index_path(tmp_path).unlink(missing_ok=True)
    loop.tools.get_definitions = MagicMock(return_value=[])
    if fake is not None:
        loop.tools.register(fake)  # replaces the real memory_search for this loop
    captured: list[list[dict]] = []

    async def _chat(*args, **kwargs):
        captured.append(kwargs.get("messages") or (args[0] if args else []))
        return LLMResponse(content="ok", tool_calls=[])

    loop.provider.chat_with_retry = AsyncMock(side_effect=_chat)
    return loop, captured, rec


def _user_content(captured: list[list[dict]]) -> str:
    msgs = captured[0]
    user = [m for m in msgs if m.get("role") == "user"][-1]
    return user["content"] if isinstance(user["content"], str) else "".join(
        b.get("text", "") for b in user["content"] if isinstance(b, dict)
    )


QUESTION = "What do you remember about Ana's bakery on Main Street?"


def test_build_messages_places_block_after_text_before_runtime_context(tmp_path: Path) -> None:
    builder = ContextBuilder(workspace=tmp_path)
    block = build_memory_context_block(_RENDERED)

    msgs = builder.build_messages(history=[], current_message="hola", channel="cli", chat_id="c",
                                  memory_prefetch=block)

    content = msgs[-1]["content"]
    assert content.index("hola") < content.index("<memory-context>") < content.index(ContextBuilder._RUNTIME_CONTEXT_TAG)
    assert "Ana runs the bakery" in content


def test_build_messages_places_block_after_text_on_the_list_content_path(tmp_path: Path) -> None:
    """With media attached the user content is a list of blocks, not a string.
    The fence keeps its place: attachments, the user's text, the block, then
    the runtime context."""
    builder = ContextBuilder(workspace=tmp_path)
    png = tmp_path / "shot.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)

    msgs = builder.build_messages(history=[], current_message="what is this?", channel="cli",
                                  chat_id="c", media=[str(png)],
                                  memory_prefetch=build_memory_context_block(_RENDERED))

    content = msgs[-1]["content"]
    assert [b["type"] for b in content] == ["image_url", "text", "text", "text"]
    assert content[1]["text"] == "what is this?"
    assert "<memory-context>" in content[2]["text"] and "Ana runs the bakery" in content[2]["text"]
    assert ContextBuilder._RUNTIME_CONTEXT_TAG in content[3]["text"]


@pytest.mark.asyncio
async def test_prefetch_runs_a_warm_search_and_fences_the_hits(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeSearch(total=1)
    loop, captured, rec = _make_loop(tmp_path, monkeypatch, fake=fake)

    await loop._process_message(InboundMessage(channel="websocket", sender_id="u", chat_id="c", content=QUESTION))

    assert fake.calls == [{"query": QUESTION, "limit": 3, "level": "warm"}]
    content = _user_content(captured)
    assert content.index(QUESTION) < content.index("<memory-context>")
    assert "Ana runs the bakery" in content and "</memory-context>" in content
    # The fake renders a "person:ana" CANONICAL marker in the fenced text;
    # this is the structural marker ctx.prefetch_refs extracts via regex.
    assert "=== CANONICAL: person:ana" in content
    stored = loop.sessions.get_or_create("websocket:c").messages
    assert all("<memory-context>" not in str(m.get("content")) for m in stored)
    prefetch = [d for t, d in rec.events if t == "memory.prefetch"]
    assert prefetch and prefetch[0]["hits"] == 1 and "skipped" not in prefetch[0]
    usage = [d for t, d in rec.events if t == "turn.memory_usage"]
    assert usage[0]["prefetch_hits"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("content,reason", [
    ("/status", "command_or_empty"),
    ("ok thanks", "short"),
])
async def test_prefetch_gates(tmp_path: Path, monkeypatch, content: str, reason: str) -> None:
    fake = _FakeSearch()
    loop, captured, rec = _make_loop(tmp_path, monkeypatch, fake=fake)
    if content.startswith("/"):
        loop.commands.dispatch = AsyncMock(return_value=None)   # let the message reach BUILD

    await loop._process_message(InboundMessage(channel="websocket", sender_id="u", chat_id="c", content=content))

    assert fake.calls == []
    assert [d for t, d in rec.events if t == "memory.prefetch"][0]["skipped"] == reason


@pytest.mark.asyncio
async def test_prefetch_skips_non_interactive_sessions_and_disabled_config(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeSearch()
    loop, captured, rec = _make_loop(tmp_path, monkeypatch, fake=fake)
    session = loop.sessions.get_or_create("websocket:node")
    session.metadata["origin_type"] = "workflow_node"
    loop.sessions.save(session)
    await loop._process_message(InboundMessage(channel="websocket", sender_id="u", chat_id="node", content=QUESTION))
    assert fake.calls == []
    assert [d for t, d in rec.events if t == "memory.prefetch"][-1]["skipped"] == "non_interactive"

    loop.app_config = SimpleNamespace(memory=SimpleNamespace(prefetch=MemoryPrefetchConfig(enabled=False)))
    await loop._process_message(InboundMessage(channel="websocket", sender_id="u", chat_id="c", content=QUESTION))
    assert fake.calls == []
    assert [d for t, d in rec.events if t == "memory.prefetch"][-1]["skipped"] == "disabled"


@pytest.mark.asyncio
@pytest.mark.parametrize("session_key,prefetched", [
    ("cron:job1:run:1", False),
    ("automation:job1:run:1", False),
    # Not on either of the runtime's lists: the gate fails open, so a bench
    # run is prefetched like an interactive channel.
    ("bench:locomo:1", True),
])
async def test_prefetch_gate_follows_the_runtime_autonomous_session_kinds(
    tmp_path: Path, monkeypatch, session_key: str, prefetched: bool
) -> None:
    # cron and automation are the runtime's own autonomous kinds; the gate
    # reads that list rather than keeping its own copy.
    assert "cron:" in AUTONOMOUS_SESSION_PREFIXES
    assert "automation:" in AUTONOMOUS_SESSION_PREFIXES
    fake = _FakeSearch()
    loop, captured, rec = _make_loop(tmp_path, monkeypatch, fake=fake)

    await loop._process_message(
        InboundMessage(channel="websocket", sender_id="u", chat_id="c", content=QUESTION),
        session_key=session_key,
    )

    row = [d for t, d in rec.events if t == "memory.prefetch"][-1]
    if prefetched:
        assert fake.calls == [{"query": QUESTION, "limit": 3, "level": "warm"}]
        assert "skipped" not in row
    else:
        assert fake.calls == []
        assert row["skipped"] == "non_interactive"


@pytest.mark.asyncio
async def test_prefetch_no_hits_and_timeout_add_nothing(tmp_path: Path, monkeypatch) -> None:
    loop, captured, rec = _make_loop(tmp_path, monkeypatch, fake=_FakeSearch(total=0))
    await loop._process_message(InboundMessage(channel="websocket", sender_id="u", chat_id="c", content=QUESTION))
    assert "<memory-context>" not in _user_content(captured)
    assert [d for t, d in rec.events if t == "memory.prefetch"][-1]["skipped"] == "no_hits"

    slow = _FakeSearch(total=1, delay=0.5)
    loop, captured, rec = _make_loop(tmp_path, monkeypatch, fake=slow)
    loop.app_config = SimpleNamespace(memory=SimpleNamespace(prefetch=MemoryPrefetchConfig(timeout_s=0.05)))
    await loop._process_message(InboundMessage(channel="websocket", sender_id="u", chat_id="c", content=QUESTION))
    assert "<memory-context>" not in _user_content(captured)
    assert [d for t, d in rec.events if t == "memory.prefetch"][-1]["skipped"] == "timeout"


@pytest.mark.asyncio
async def test_prefetch_block_is_cut_at_max_chars(tmp_path: Path, monkeypatch) -> None:
    # Longer than the smallest max_chars the config accepts, so the cut fires.
    fake = _FakeSearch(total=1, rendered=_RENDERED + "\nAna bakes rye every morning." * 20)
    loop, captured, rec = _make_loop(tmp_path, monkeypatch, fake=fake)
    loop.app_config = SimpleNamespace(memory=SimpleNamespace(prefetch=MemoryPrefetchConfig(max_chars=200)))
    await loop._process_message(InboundMessage(channel="websocket", sender_id="u", chat_id="c", content=QUESTION))
    content = _user_content(captured)
    block = content[content.index("<memory-context>"):content.index("</memory-context>")]
    assert "(truncated" in block and len(block) < 200 + 400


@pytest.mark.asyncio
async def test_prefetch_gate_no_index(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeSearch()
    loop, captured, rec = _make_loop(tmp_path, monkeypatch, fake=fake, with_index=False)

    await loop._process_message(InboundMessage(channel="websocket", sender_id="u", chat_id="c", content=QUESTION))

    assert fake.calls == []
    assert [d for t, d in rec.events if t == "memory.prefetch"][-1]["skipped"] == "no_index"


@pytest.mark.asyncio
async def test_prefetch_gate_no_tool(tmp_path: Path, monkeypatch) -> None:
    loop, captured, rec = _make_loop(tmp_path, monkeypatch)
    loop.tools.unregister("memory_search")

    await loop._process_message(InboundMessage(channel="websocket", sender_id="u", chat_id="c", content=QUESTION))

    assert [d for t, d in rec.events if t == "memory.prefetch"][-1]["skipped"] == "no_tool"


@pytest.mark.asyncio
async def test_timeout_backs_the_prefetch_off_for_the_next_turns(tmp_path: Path, monkeypatch) -> None:
    """``asyncio.wait_for`` abandons the search thread, so a search that times
    out every turn would strand a default-executor worker per turn. After a
    timeout the prefetch stops trying until the backoff window passes."""
    slow = _FakeSearch(total=1, delay=0.5)
    loop, captured, rec = _make_loop(tmp_path, monkeypatch, fake=slow)
    loop.app_config = SimpleNamespace(
        memory=SimpleNamespace(prefetch=MemoryPrefetchConfig(timeout_s=0.05, backoff_s=60.0))
    )

    await loop._process_message(InboundMessage(channel="websocket", sender_id="u", chat_id="c", content=QUESTION))
    assert [d for t, d in rec.events if t == "memory.prefetch"][-1]["skipped"] == "timeout"
    assert len(slow.calls) == 1

    await loop._process_message(InboundMessage(channel="websocket", sender_id="u", chat_id="c", content=QUESTION))
    assert [d for t, d in rec.events if t == "memory.prefetch"][-1]["skipped"] == "backoff"
    assert len(slow.calls) == 1          # the search was not attempted again


@pytest.mark.asyncio
async def test_backoff_zero_keeps_searching_every_turn(tmp_path: Path, monkeypatch) -> None:
    slow = _FakeSearch(total=1, delay=0.5)
    loop, captured, rec = _make_loop(tmp_path, monkeypatch, fake=slow)
    loop.app_config = SimpleNamespace(
        memory=SimpleNamespace(prefetch=MemoryPrefetchConfig(timeout_s=0.05, backoff_s=0))
    )

    for _ in range(2):
        await loop._process_message(
            InboundMessage(channel="websocket", sender_id="u", chat_id="c", content=QUESTION)
        )

    assert len(slow.calls) == 2
    assert [d["skipped"] for _t, d in rec.events if _t == "memory.prefetch"] == ["timeout", "timeout"]


@pytest.mark.asyncio
async def test_error_response_is_not_a_no_hits_turn(tmp_path: Path, monkeypatch) -> None:
    """A tool that answers with an error dict failed; recording it as
    ``no_hits`` would hide the failure and keep re-running it every turn."""
    broken = _ErrorDictSearch()
    loop, captured, rec = _make_loop(tmp_path, monkeypatch, fake=broken)

    await loop._process_message(InboundMessage(channel="websocket", sender_id="u", chat_id="c", content=QUESTION))
    assert [d for t, d in rec.events if t == "memory.prefetch"][-1]["skipped"] == "error"

    await loop._process_message(InboundMessage(channel="websocket", sender_id="u", chat_id="c", content=QUESTION))
    assert [d for t, d in rec.events if t == "memory.prefetch"][-1]["skipped"] == "backoff"
    assert len(broken.calls) == 1


@pytest.mark.asyncio
async def test_prefetch_hands_its_refs_to_the_search_tool_for_the_turn(
    tmp_path: Path, monkeypatch
) -> None:
    """The model's own search in the same turn must not re-render what the
    prefetch already fenced into the message; the loop binds the turn's refs
    into the ContextVar memory_search reads for its dedup, and resets it at
    save time. Both functions are imported locally where the loop calls them
    (a top-level import would cycle back through durin.agent.tools._telemetry
    into durin.agent's own package init), so the patch targets the source
    module rather than durin.agent.loop's namespace."""
    seen: list[frozenset[str]] = []
    # The markers as the real tool renders them: canonical hits carry the
    # display uri, fragments the entry path with its suffix.
    rendered = (
        "=== CANONICAL: memory/entity_page/person:ana (consolidated 2026-01-01) ===\n"
        "Ana runs the bakery on Main St.\n=== END CANONICAL ===\n\n"
        "=== FRAGMENT: memory/episodic/2026-01-01-bakery.md (ts 2026-01-01) ===\n"
        "Ana opened the bakery in March.\n=== END FRAGMENT ==="
    )
    fake = _FakeSearch(total=2, rendered=rendered)
    loop, captured, rec = _make_loop(tmp_path, monkeypatch, fake=fake)

    def _recording_bind(refs):
        seen.append(frozenset(refs))
        return frozenset(refs)  # stand-in token; this test only checks the handoff

    def _recording_reset(token):
        seen.append(frozenset())

    monkeypatch.setattr("durin.agent.tools.memory_search.bind_turn_prefetch_refs", _recording_bind)
    monkeypatch.setattr("durin.agent.tools.memory_search.reset_turn_prefetch_refs", _recording_reset)

    await loop._process_message(InboundMessage(channel="websocket", sender_id="u", chat_id="c", content=QUESTION))

    # Reduced to the key shape the dedup matches hits on — not the rendered
    # display uris.
    assert seen == [
        frozenset({"person:ana", "memory/episodic/2026-01-01-bakery"}),
        frozenset(),
    ]


@pytest.mark.asyncio
async def test_concurrent_sessions_never_leak_prefetch_refs_across_each_other(
    tmp_path: Path, monkeypatch
) -> None:
    """One memory_search tool is shared by the whole AgentLoop, and the
    loop's concurrency model is per-session serial / cross-session concurrent
    (AgentLoop._dispatch: a per-session asyncio.Lock, nothing serializes
    different sessions). If the turn's prefetch refs lived on the tool
    instance, a session B search landing while session A's refs were still
    bound would inherit them and wrongly collapse a hit B's own turn never
    showed. The ContextVar binds per asyncio task instead, so two turns on
    different sessions running genuinely concurrently (asyncio.gather) must
    never see each other's refs.

    Three asyncio.Events pin the exact overlap the bug needs, in order:
    ``b_bound`` guarantees session B has already bound its OWN ref
    ("person:bo") before session A binds — otherwise, under the pre-fix
    shared-attribute bug, A's write could land first and then be masked by
    B's own subsequent bind, hiding the leak from the very check meant to
    catch it. ``a_bound`` then guarantees A has bound "person:ana" before B's
    cross-session search reads anything, and ``b_searched`` guarantees A has
    not reset yet (still mid-RUN, blocked on its own model call) when that
    read happens. Without forcing this exact order, the two turns' bound
    windows might never coincide the right way and the assertion would pass
    by scheduling luck alone — on the old, buggy code as much as the new one
    (verified: temporarily swapping the ContextVar for a plain shared
    variable — the pre-fix instance-attribute semantics — made this test
    fail as expected; reverted after confirming it).
    """
    from durin.agent.tools import memory_search as memory_search_module

    b_bound = asyncio.Event()
    a_bound = asyncio.Event()
    b_searched = asyncio.Event()

    class _ContextAwareSearch:
        """Stands in for the shared memory_search tool. A query containing
        "Ana"/"Bo" simulates a turn's own automatic prefetch; the literal ref
        "person:ana" simulates the model choosing to search for that person
        mid-turn, answered by checking the SAME ContextVar the real tool's
        dedup block reads — the mechanism this test exists to verify."""

        name = "memory_search"
        description = "test double"
        parameters: dict = {}
        concurrency_safe = False

        def __init__(self) -> None:
            self.calls: list[tuple[dict, dict]] = []

        def cast_params(self, params: dict) -> dict:
            return params

        def validate_params(self, params: dict) -> list[str]:
            return []

        def to_schema(self) -> dict:
            # The background post-save consolidation check
            # (Consolidator.estimate_session_prompt_tokens) reads every
            # registered tool's schema independently of the chat mock above,
            # so this fake needs one too or that best-effort background task
            # logs a spurious AttributeError.
            return {"type": "function", "function": {
                "name": self.name, "description": self.description, "parameters": self.parameters,
            }}

        async def execute(self, **kwargs):
            query = kwargs.get("query", "")
            if query == "person:ana":
                # Session B's mid-turn search for session A's ref. Wait for
                # A to have actually bound first, so the check below is
                # meaningful, then release A to finish (and reset) only
                # after this has run — pinning the overlap deterministically.
                await a_bound.wait()
                seen = memory_search_module._turn_prefetch_refs.get()
                if query in seen:
                    result = {
                        "total": 1, "already_in_context": [query],
                        "sectioned_rendered": "## Matches shown in your Memory sections\n",
                    }
                else:
                    result = {
                        "total": 1, "sectioned_rendered":
                            f"=== CANONICAL: {query} (complete) ===\nhit\n=== END CANONICAL ===",
                    }
                b_searched.set()
            elif "Ana" in query:
                # Session A's own prefetch. Wait for B to have already bound
                # its own ref first (see the docstring above for why the
                # order matters), then land the hit that leads to A's bind.
                await b_bound.wait()
                result = {
                    "total": 1, "sectioned_rendered":
                        "=== CANONICAL: person:ana (complete) ===\nAna runs the bakery.\n=== END CANONICAL ===",
                }
            elif "Bo" in query:
                result = {
                    "total": 1, "sectioned_rendered":
                        "=== CANONICAL: person:bo (complete) ===\nBo runs the shop.\n=== END CANONICAL ===",
                }
            else:
                result = {"total": 0, "sectioned_rendered": ""}
            self.calls.append((dict(kwargs), result))
            return result

    fake = _ContextAwareSearch()
    loop, _captured, _rec = _make_loop(tmp_path, monkeypatch, fake=fake)

    real_bind = memory_search_module.bind_turn_prefetch_refs

    def _bind_and_signal(refs):
        token = real_bind(refs)
        if "person:bo" in refs:
            b_bound.set()
        if "person:ana" in refs:
            a_bound.set()
        return token

    monkeypatch.setattr("durin.agent.tools.memory_search.bind_turn_prefetch_refs", _bind_and_signal)

    call_counts: dict[str, int] = {}

    async def _chat(*args, **kwargs):
        messages = kwargs.get("messages") or (args[0] if args else [])
        session = "a" if "Ana" in _user_content([messages]) else "b"
        call_counts[session] = call_counts.get(session, 0) + 1
        if session == "a":
            # A must not reach SAVE (and reset its bound ref) before B's
            # cross-session search has already run against it.
            await b_searched.wait()
            return LLMResponse(content="Done", tool_calls=[])
        if call_counts["b"] == 1:
            return LLMResponse(content="", finish_reason="tool_calls", tool_calls=[
                ToolCallRequest(id="call-b", name="memory_search", arguments={"query": "person:ana"}),
            ])
        return LLMResponse(content="Done", tool_calls=[])

    loop.provider.chat_with_retry = AsyncMock(side_effect=_chat)

    async def _run(chat_id: str, text: str) -> frozenset[str]:
        await loop._process_message(
            InboundMessage(channel="websocket", sender_id="u", chat_id=chat_id, content=text)
        )
        # Read in the SAME task _process_message just ran in (a direct
        # await, not a child task), so this is that turn's own post-SAVE
        # ContextVar state — not the gather-caller's, which a task's own
        # binding never touches regardless of whether the reset ran.
        return memory_search_module._turn_prefetch_refs.get()

    after_a, after_b = await asyncio.gather(
        _run("a", "What do you remember about Ana's bakery on Main Street?"),
        _run("b", "What do you remember about Bo's shop across town?"),
    )

    b_cross_call = next(c for c in fake.calls if c[0].get("query") == "person:ana")
    _kwargs, b_result = b_cross_call
    assert "person:ana" not in b_result.get("already_in_context", [])

    # Each turn's own binding is released by the time it returns, in its own
    # task — not just eventually, and not because nobody else was watching.
    assert after_a == frozenset()
    assert after_b == frozenset()


@pytest.mark.asyncio
async def test_prefetch_gate_error_still_replies(tmp_path: Path, monkeypatch) -> None:
    loop, captured, rec = _make_loop(tmp_path, monkeypatch, fake=_RaisingSearch())

    reply = await loop._process_message(
        InboundMessage(channel="websocket", sender_id="u", chat_id="c", content=QUESTION)
    )

    assert "<memory-context>" not in _user_content(captured)
    assert [d for t, d in rec.events if t == "memory.prefetch"][-1]["skipped"] == "error"
    # A broken search must not break the turn: the provider still ran and replied.
    assert captured and reply is not None
