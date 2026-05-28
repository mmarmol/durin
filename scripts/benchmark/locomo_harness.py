"""LoCoMo per-QA harness: isolated workspace + memory seed + agent run + trace.

For each QA:

1. Build a fresh workspace under ``bench-workspaces/locomo/<run>/<qa_id>/``
   so the benchmark doesn't pollute the user's real ``~/.durin/workspace/``.
2. Seed memory with the conversation transcript — every turn becomes an
   episodic entry tagged with the speaker. We do this in bulk via
   ``store_memory`` (not through the agent) — orders of magnitude faster
   than running the agent through each turn, and the published winners
   (Mem0, HyperMem) also seed in bulk.
3. Bind a per-QA :class:`TelemetryLogger` so EVERY event durin emits
   while answering the question lands in a file we own and can attach
   to the trace.
4. Run the agent loop once with the QA as user input.
5. Capture: final answer, tool calls (with args + results), telemetry
   events, context size, iteration count, stop reason.
6. Return a :class:`QATrace` the runner persists.

The harness is deliberately stateless across QAs — every call gets a
fresh workspace + bound telemetry. Re-running a QA is the same code
path as running it for the first time (essential for replay).
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import functools

from durin.memory.provenance import author_scope
from durin.memory.store import store_memory


def _agent_seeded(fn):
    """Decorator: wrap a seeder function in author_scope("agent_created").

    LoCoMo bulk seeders model agent observations (the conversation
    turns we replay as if the agent stored them via memory_store).
    Per ``durin/memory/provenance.py`` every write must declare its
    author; this decorator carries that declaration once per seeder.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with author_scope("agent_created"):
            return fn(*args, **kwargs)
    return wrapper
from durin.telemetry.logger import TelemetryLogger, bind_telemetry, reset_telemetry

from scripts.benchmark.locomo_dataset import QA, Conversation

__all__ = ["QATrace", "run_qa"]

logger = logging.getLogger(__name__)

# Hard cap on per-QA wall-clock. Some QAs trip the agent into a loop
# (LoCoMo adversarial category is designed to do that). Without this
# cap a single bad question can stall the whole subset run.
DEFAULT_PER_QA_TIMEOUT_S = 90

# Max iterations per QA. The agent should typically answer in 1-3
# iterations (one tool call + final answer). 8 is generous; >8 almost
# always means a loop the model can't escape.
DEFAULT_MAX_ITERATIONS = 8


@dataclass
class QATrace:
    """Everything we capture for one QA. Saved as JSON per QA so
    failures can be inspected, replayed, and aggregated post-hoc."""

    qa_id: str
    conv_id: str
    category: str
    question: str
    expected: str
    got: str
    duration_s: float
    iterations: int
    stop_reason: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    context_chars_final: int = 0
    workspace_path: str = ""
    telemetry_path: str = ""  # relative path within the run dir
    error: str | None = None  # populated when the run itself raised

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


async def run_qa(
    qa: QA,
    *,
    workspace_root: Path,
    telemetry_path: Path,
    model: str = "glm-5-turbo",
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    timeout_s: float = DEFAULT_PER_QA_TIMEOUT_S,
    enable_memory: bool = True,
    log_path: Path | None = None,
) -> QATrace:
    """Run one QA end-to-end and return its trace.

    ``workspace_root`` becomes the agent's workspace for this QA. It
    will be created fresh (any pre-existing contents wiped) so the run
    is sealed off from the user's real workspace.

    ``telemetry_path`` is where the per-QA JSONL telemetry file lands.
    The parent dir is created if missing.

    ``log_path`` (optional) gets stdout/stderr-style debug log lines
    if the agent emits any during the run.
    """
    workspace_root = Path(workspace_root)
    telemetry_path = Path(telemetry_path)
    # Wipe + create — guarantees isolation.
    if workspace_root.exists():
        shutil.rmtree(workspace_root)
    workspace_root.mkdir(parents=True, exist_ok=True)
    telemetry_path.parent.mkdir(parents=True, exist_ok=True)

    # Seed memory with the conversation transcript before asking the QA.
    # Skipped when enable_memory=False (ablation baseline — measures how
    # much the memory layer actually contributes vs. answering cold).
    if enable_memory and qa.conversation is not None:
        _seed_memory_from_conversation(workspace_root, qa.conversation)
        # Build the vector index over the seeded files. Without this
        # `memory_search` falls to substring grep over the full natural-
        # language query and returns 0 for queries like "Calvin Japan
        # stay" (verified in 12cc1897 run). The bench is not a clean
        # install — we want the full retrieval stack active.
        _build_vector_index(workspace_root)

    # Bind per-QA telemetry. Every memory.recall / memory.store /
    # tool.* / cache.usage / compaction.* event durin emits while
    # answering will land in this file.
    bench_logger = TelemetryLogger(telemetry_path)
    token = bind_telemetry(bench_logger)

    trace = QATrace(
        qa_id=qa.qa_id,
        conv_id=qa.conv_id,
        category=qa.category,
        question=qa.question,
        expected=qa.answer,
        got="",
        duration_s=0.0,
        iterations=0,
        stop_reason="",
        workspace_path=str(workspace_root),
        telemetry_path=str(telemetry_path.name),
    )

    started = time.monotonic()
    try:
        await asyncio.wait_for(
            _ask_agent(qa, workspace_root, model, max_iterations, trace,
                       enable_memory=enable_memory),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        trace.stop_reason = "timeout"
        trace.error = f"per-QA timeout {timeout_s:.0f}s exceeded"
    except Exception as exc:  # noqa: BLE001
        logger.exception("QA %s raised", qa.qa_id)
        trace.stop_reason = "exception"
        trace.error = f"{type(exc).__name__}: {exc}"
    finally:
        reset_telemetry(token)
        trace.duration_s = time.monotonic() - started
        # Audit H3 (2026-05-29): the AgentLoop overrides our
        # `bench_logger` binding with its own per-session logger
        # (`durin/agent/loop.py::_main_loop` calls
        # ``bind_telemetry(session_logger)`` unconditionally), so every
        # memory.recall / memory.store / cache.usage event lands in
        # ``~/.cache/durin/telemetry/bench_<qa_id>_<date>.jsonl`` rather
        # than the per-QA bench file. Without this merge the run_dir's
        # ``telemetry/`` directory carries only the harness's own bind
        # window (embedding load + index rebuild + 2 composition rows)
        # and post-bench analysis has to chase events across two
        # directories. Append the session log into the per-QA file so
        # the bench artefact is self-contained.
        _merge_session_telemetry_into(telemetry_path, qa.qa_id, started)
    return trace


def _merge_session_telemetry_into(
    telemetry_path: Path, qa_id: str, started_monotonic: float,
) -> None:
    """Append events from the session-scoped telemetry file into the
    per-QA bench file. Best-effort: failures degrade silently.
    """
    try:
        from durin.telemetry.logger import get_session_logger
        # `get_session_logger` is the same path the AgentLoop uses,
        # so we reuse it to compute the exact destination filename
        # rather than reconstructing the sanitiser by hand.
        session_logger = get_session_logger(f"bench:{qa_id}")
        session_path = Path(session_logger.path)
    except Exception:  # noqa: BLE001
        return
    if not session_path.is_file() or session_path == telemetry_path:
        return
    try:
        existing = telemetry_path.read_text(encoding="utf-8") \
            if telemetry_path.exists() else ""
        with session_path.open("r", encoding="utf-8") as src:
            lines = src.readlines()
        merged = existing + "".join(lines)
        telemetry_path.write_text(merged, encoding="utf-8")
    except Exception:  # noqa: BLE001
        return


@_agent_seeded
def _seed_memory_from_conversation(
    workspace_root: Path, conv: Conversation,
) -> None:
    """Bulk-seed memory with every turn, image caption, and LoCoMo
    sample-level summary.

    Each turn becomes one episodic entry tagged with
    ``person:<speaker_slug>``. When the turn carries a ``blip_caption``
    or ``query`` (image-bearing turn), those are appended to the text
    so the visual context is searchable (see doc 28 §4.5 — 20.8% of
    turns had blip_caption silently dropped).

    Additionally seeds the LoCoMo sample-level fields:
    - ``event_summary[events_session_N]`` → one episodic entry per
      session-event pair, dated with the session's date_time so the
      vector index gets curator-provided event abstractions (e.g.
      "John's car windshield is shattered" — a fact that lived nowhere
      in the raw turn transcript for ``conv-2-q19``).
    - ``observation`` → one entry per session+speaker.
    - ``session_summary`` → one entry per session.

    Bulk seeding (not running the agent through the conversation) is
    standard for memory benchmarks — it factors out the agent's
    write-side noise and exercises only the read-side retrieval that
    LoCoMo actually measures.
    """
    speaker_slugs = {
        conv.speaker_a: _slug(conv.speaker_a),
        conv.speaker_b: _slug(conv.speaker_b),
    }
    for session in conv.sessions:
        valid_from = _try_parse_session_date(session.date_time)
        source_ref = f"sessions/{conv.conv_id}_s{session.index}.md"
        for turn in session.turns:
            speaker = turn.get("speaker") or "unknown"
            text = turn.get("text") or ""
            # Append image context (blip_caption + query) so the vector
            # index covers it. Same entry — keeps the agent's view of
            # "what was said in this turn" complete.
            text_with_visuals = _enrich_turn_with_visuals(turn, text)
            if not text_with_visuals.strip():
                continue
            slug = speaker_slugs.get(speaker) or _slug(speaker)
            entity_ref = f"person:{slug}"
            try:
                store_memory(
                    workspace_root,
                    content=text_with_visuals,
                    headline=f"{speaker}: {text_with_visuals[:60]}",
                    entities=[entity_ref],
                    source_refs=[source_ref],
                    valid_from=valid_from,
                )
            except Exception:  # noqa: BLE001
                logger.exception("seed failure for conv %s session %d",
                                  conv.conv_id, session.index)

    # Sample-level summaries — separate from per-turn entries so the
    # agent can retrieve them as their own context blocks. Headlines are
    # tagged "summary:" so the LLM can see they're curator-derived.
    _seed_event_summaries(workspace_root, conv, speaker_slugs)
    _seed_observations(workspace_root, conv, speaker_slugs)
    _seed_session_summaries(workspace_root, conv)


def _enrich_turn_with_visuals(turn: dict, text: str) -> str:
    """Append blip_caption + query to text when the turn has an image.

    Format: original text on its own line, then `[image: <caption>]`
    and `[image_query: <query>]` lines. The LLM sees them as inline
    annotations and the vector embedding picks up the visual nouns
    (the conv-2-q19 'windshield' case).
    """
    parts = [text] if text else []
    cap = (turn.get("blip_caption") or "").strip()
    if cap:
        parts.append(f"[image: {cap}]")
    q = (turn.get("query") or "").strip()
    if q:
        parts.append(f"[image_query: {q}]")
    return "\n".join(parts)


@_agent_seeded
def _seed_event_summaries(
    workspace_root: Path, conv: Conversation,
    speaker_slugs: dict[str, str],
) -> None:
    """Seed ``event_summary.events_session_N`` per speaker per session.

    The LoCoMo curator produced these as third-person abstractions of
    what happened during a session ("John suffers from an accident
    where his car's windshield is shattered"). They cover facts that
    sometimes aren't explicit in the raw turns.
    """
    summaries = conv.event_summary or {}
    # session_idx → date for valid_from
    session_dates = {
        s.index: _try_parse_session_date(s.date_time) for s in conv.sessions
    }
    for key, events_by_speaker in summaries.items():
        # key format: "events_session_N"
        n = _extract_session_index(key)
        if n is None or not isinstance(events_by_speaker, dict):
            continue
        valid_from = session_dates.get(n)
        source_ref = f"sessions/{conv.conv_id}_s{n}_event_summary.md"
        for speaker, events in events_by_speaker.items():
            if not isinstance(events, list):
                continue
            slug = speaker_slugs.get(speaker) or _slug(speaker)
            for event in events:
                event_text = str(event).strip()
                if not event_text:
                    continue
                try:
                    store_memory(
                        workspace_root,
                        content=event_text,
                        headline=f"event[{speaker}]: {event_text[:60]}",
                        entities=[f"person:{slug}"],
                        source_refs=[source_ref],
                        valid_from=valid_from,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("event_summary seed failure %s s%d",
                                     conv.conv_id, n)


@_agent_seeded
def _seed_observations(
    workspace_root: Path, conv: Conversation,
    speaker_slugs: dict[str, str],
) -> None:
    """Seed ``observation`` block — typically per-session per-speaker
    observations the curator wrote about each side of the conversation."""
    obs = conv.observation or {}
    session_dates = {
        s.index: _try_parse_session_date(s.date_time) for s in conv.sessions
    }
    for key, by_speaker in obs.items():
        n = _extract_session_index(key)
        valid_from = session_dates.get(n) if n is not None else None
        source_ref = f"sessions/{conv.conv_id}_observation_{key}.md"
        if not isinstance(by_speaker, dict):
            continue
        for speaker, observations in by_speaker.items():
            slug = speaker_slugs.get(speaker) or _slug(speaker)
            items = observations if isinstance(observations, list) else [observations]
            for item in items:
                text = str(item).strip()
                if not text:
                    continue
                try:
                    store_memory(
                        workspace_root,
                        content=text,
                        headline=f"observation[{speaker}]: {text[:60]}",
                        entities=[f"person:{slug}"],
                        source_refs=[source_ref],
                        valid_from=valid_from,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("observation seed failure %s", conv.conv_id)


@_agent_seeded
def _seed_session_summaries(workspace_root: Path, conv: Conversation) -> None:
    """Seed ``session_summary`` block — one per session with a brief
    summary of what was discussed."""
    summaries = conv.session_summary or {}
    session_dates = {
        s.index: _try_parse_session_date(s.date_time) for s in conv.sessions
    }
    for key, summary in summaries.items():
        n = _extract_session_index(key)
        if n is None:
            continue
        valid_from = session_dates.get(n)
        source_ref = f"sessions/{conv.conv_id}_s{n}_summary.md"
        text = str(summary).strip() if not isinstance(summary, (list, dict)) else json.dumps(summary)
        if not text or text in ("{}", "[]"):
            continue
        try:
            store_memory(
                workspace_root,
                content=text,
                headline=f"session-{n} summary: {text[:60]}",
                entities=[],
                source_refs=[source_ref],
                valid_from=valid_from,
            )
        except Exception:  # noqa: BLE001
            logger.exception("session_summary seed failure %s s%d", conv.conv_id, n)


def _extract_session_index(key: str) -> int | None:
    """Pull the N from keys like 'events_session_4', 'session_4_observation'."""
    import re
    m = re.search(r"session_(\d+)", key or "")
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


def _build_vector_index(workspace: Path) -> None:
    """Embed every seeded ``memory/<class>/*.md`` so vector search works.

    Bulk-seeded entries (`store_memory`) skip the index by design —
    only the tool path (`memory_store`) embeds at write time. For the
    bench we want vector retrieval active, so we rebuild over the whole
    workspace once after the seed.

    Silent no-op when ``lancedb`` is unavailable in the env — the agent
    still has the grep fallback. The first call triggers the embedding
    model download (~400 MB, cached at ``~/.cache/fastembed``).
    """
    from durin.config.loader import load_config
    from durin.memory.vector_index import VectorIndex, vector_index_available

    if not vector_index_available():
        logger.warning(
            "vector index unavailable (lancedb not installed); bench will "
            "fall back to grep retrieval"
        )
        return
    cfg = load_config()
    try:
        from durin.memory.embedding import FastembedProvider

        provider = FastembedProvider(model=cfg.memory.embedding.model)
        vi = VectorIndex(workspace, provider)
        n = vi.rebuild_from_workspace()
        logger.info("vector index built: %d entries in %s", n, workspace)
    except Exception:  # noqa: BLE001
        # Single bad index build must NOT kill the QA — agent still has
        # grep + filesystem grep as fallbacks.
        logger.exception("vector index build failed for %s", workspace)


def _slug(name: str) -> str:
    """Lowercase + non-alphanum → underscore. Stable across runs."""
    out: list[str] = []
    for ch in name.lower():
        out.append(ch if ch.isalnum() else "_")
    return "".join(out).strip("_") or "unknown"


def _try_parse_session_date(raw: str) -> Any:
    """LoCoMo session timestamps vary in format. Best-effort parse to
    a ``date`` — return None silently on failure rather than blocking
    the seed pass."""
    import datetime as _dt

    if not raw:
        return None
    for fmt in (
        "%I:%M %p on %d %B, %Y",
        "%I:%M %p on %d %b, %Y",
        "%I:%M %p, %d %B %Y",
        "%I:%M %p, %d %b %Y",
        "%H:%M %d %B, %Y",
        "%d %B %Y",
        "%d %b %Y",
        "%Y-%m-%d",
    ):
        try:
            return _dt.datetime.strptime(raw.strip(), fmt).date()
        except ValueError:
            continue
    return None


async def _ask_agent(
    qa: QA,
    workspace: Path,
    model: str,
    max_iterations: int,
    trace: QATrace,
    *,
    enable_memory: bool = True,
) -> None:
    """Drive durin's agent loop to answer the question.

    Uses the public bus surface (publish_inbound + consume_outbound)
    so we exercise the same code path real channels do. The agent
    loop runs as a background task; we drain outbound messages until
    we see a non-stream final answer for our chat_id, then cancel.

    The session_key is set via :attr:`InboundMessage.session_key_override`
    so memory.recall / memory.store / cache.usage events emitted during
    the dispatch carry our bench-specific key — useful when the user
    wants to grep the global telemetry cache after a benchmark.
    """
    from durin.agent.loop import AgentLoop
    from durin.bus.events import InboundMessage
    from durin.bus.queue import MessageBus
    from durin.config.loader import load_config

    cfg = load_config()
    # Force the benchmark workspace into the active config so memory_*
    # tools resolve against the isolated tree instead of
    # ~/.durin/workspace. Iteration cap is conservative — benchmark
    # wants signal, not loops.
    cfg.agents.defaults.workspace = str(workspace)
    if model:
        cfg.agents.defaults.model = model
    cfg.agents.defaults.max_tool_iterations = max_iterations
    # Activate vector retrieval for the bench. The durin default is opt-in
    # (the embedding model is ~400 MB) but the bench is not a clean
    # install — we want the full retrieval stack so memory_search uses
    # semantic similarity instead of literal substring match.
    if enable_memory:
        cfg.memory.enabled = True

    bus = MessageBus()
    loop_agent = AgentLoop.from_config(cfg, bus=bus)

    # Ablation mode: strip every memory tool so the agent has zero
    # read/write access to the memory layer — not just an empty workspace.
    # This is a stricter baseline than --no-memory seeding alone: the LLM
    # never sees memory_search / memory_store / memory_drill / memory_ingest
    # in its tool list, so it cannot self-seed or bias its reasoning around
    # the memory API surface.
    if not enable_memory:
        for tool_name in ("memory_search", "memory_store", "memory_drill", "memory_ingest"):
            loop_agent.tools.unregister(tool_name)

    session_key = f"bench:{qa.qa_id}"
    msg = InboundMessage(
        channel="bench",
        sender_id="locomo",
        chat_id=qa.qa_id,
        content=qa.question,
        session_key_override=session_key,
    )

    # Start the loop running so it can consume our inbound message.
    loop_task = asyncio.create_task(loop_agent.run())
    answer_parts: list[str] = []

    async def _drain_until_final() -> None:
        """Consume outbound messages until the final non-stream content.

        Skips intermediate progress / retry / streaming messages — only
        the final assistant turn counts as the answer for benchmark
        scoring. These markers come from durin's outbound conventions
        (see :meth:`AgentLoop._build_retry_wait_callback` for retry,
        progress_hook for streaming).
        """
        while True:
            out = await bus.consume_outbound()
            if out.chat_id != qa.qa_id:
                continue  # not ours (shouldn't happen in benchmark)
            meta = out.metadata or {}
            # Skip every intermediate signal — retries, progress
            # heartbeats, stream deltas, end-of-stream markers, system
            # notes. The final assistant turn has none of these flags.
            if meta.get("_stream_delta"):
                continue
            if meta.get("_streamed"):
                if answer_parts:
                    return
                continue
            if meta.get("_retry_wait"):
                continue
            if meta.get("_progress"):
                continue
            if meta.get("_status"):
                continue
            if meta.get("render_as") == "text" and answer_parts:
                continue
            content = out.content or ""
            if content:
                answer_parts.append(content)
                return
            return

    try:
        await bus.publish_inbound(msg)
        await asyncio.wait_for(_drain_until_final(), timeout=DEFAULT_PER_QA_TIMEOUT_S)
    finally:
        # Stop the loop so it doesn't leak background tasks. Setting
        # _running=False lets the `while self._running` exit on the
        # next iteration; we still cancel for safety in case it's
        # mid-await.
        loop_agent._running = False
        loop_task.cancel()
        try:
            await loop_task
        except (asyncio.CancelledError, Exception):
            pass

    trace.got = " ".join(p.strip() for p in answer_parts if p.strip())
    trace.stop_reason = trace.stop_reason or "ok"
    # Capture tool calls from session messages. ``_load`` is the
    # private accessor that returns the in-memory Session or reads it
    # back from disk if it was evicted between the dispatch and our
    # post-mortem. ``get_or_create`` works too but would silently
    # create an empty record for a key that didn't run.
    try:
        session = loop_agent.sessions._load(session_key)
        if session is not None:
            trace.tool_calls = _extract_tool_calls(session.messages)
            trace.iterations = _count_assistant_iterations(session.messages)
            trace.context_chars_final = _estimate_context_chars(session.messages)
    except Exception:  # noqa: BLE001
        logger.exception("trace enrichment failed for %s", qa.qa_id)


def _extract_tool_calls(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten tool_calls from the assistant messages + their results."""
    calls: list[dict[str, Any]] = []
    pending: dict[str, dict[str, Any]] = {}
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls") or []:
                tc_id = tc.get("id") or tc.get("call_id") or ""
                fn = (tc.get("function") or {})
                pending[tc_id] = {
                    "id": tc_id,
                    "tool": fn.get("name") or "",
                    "args": fn.get("arguments") or "",
                }
        elif msg.get("role") == "tool":
            tc_id = msg.get("tool_call_id") or ""
            entry = pending.pop(tc_id, None) or {"id": tc_id, "tool": "", "args": ""}
            result = msg.get("content") or ""
            if isinstance(result, list):
                result = json.dumps(result)[:2000]
            entry["result_preview"] = str(result)[:2000]
            calls.append(entry)
    # Any tool calls without a matching result (model error / interruption)
    # still get recorded so the analyzer sees them.
    for entry in pending.values():
        entry["result_preview"] = "(no result captured)"
        calls.append(entry)
    return calls


def _count_assistant_iterations(messages: list[dict[str, Any]]) -> int:
    return sum(1 for m in messages if isinstance(m, dict) and m.get("role") == "assistant")


def _estimate_context_chars(messages: list[dict[str, Any]]) -> int:
    total = 0
    for m in messages:
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and "text" in part:
                    total += len(str(part["text"]))
    return total
