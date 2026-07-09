"""Channel manager for coordinating chat channels."""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from durin.bus.events import InboundMessage, OutboundMessage
from durin.bus.queue import MessageBus
from durin.channels.base import BaseChannel
from durin.config.schema import Config
from durin.pairing import PAIRING_CODE_META_KEY, format_pairing_reply, generate_code
from durin.utils.restart import consume_restart_notice_from_env, format_restart_completed_message

if TYPE_CHECKING:
    from durin.cron.service import CronService
    from durin.session.manager import SessionManager


def _default_webui_dist() -> Path | None:
    """Return the absolute path to the bundled webui dist directory if it exists."""
    try:
        import durin.web as web_pkg  # type: ignore[import-not-found]
    except ImportError:
        return None
    candidate = Path(web_pkg.__file__).resolve().parent / "dist"
    return candidate if candidate.is_dir() else None


# Retry delays for message sending (exponential backoff: 1s, 2s, 4s)
_SEND_RETRY_DELAYS = (1, 2, 4)
# How long hot-start waits for channel.start() to fail fast before parking it
# as a background task (long-running start() loops, e.g. Slack's keepalive).
_HOT_START_FAIL_FAST_WINDOW_S = 2.0
# Crash-supervision backoff for _start_channel: exponential doubling capped at
# this delay, reset once a channel has stayed up for _CHANNEL_STABLE_UPTIME_S.
_CHANNEL_RESTART_MAX_DELAY_S = 60.0
_CHANNEL_STABLE_UPTIME_S = 300.0

_BOOL_CAMEL_ALIASES: dict[str, str] = {
    "send_progress": "sendProgress",
    "send_tool_hints": "sendToolHints",
    "show_reasoning": "showReasoning",
}

def _resolve_section_secrets(section: Any) -> Any:
    """Deep-copy a channel config section, resolving ``${secret:}`` refs.

    Channel credentials (tokens, keys) are stored as references; this
    resolves them into a copy passed to the channel, so the plaintext
    never lives in the shared ``Config`` object.
    """
    from durin.security.secrets import resolve_secret

    if isinstance(section, dict):
        return {k: _resolve_section_secrets(v) for k, v in section.items()}
    if isinstance(section, list):
        return [_resolve_section_secrets(v) for v in section]
    if isinstance(section, str):
        return resolve_secret(section)
    return section


class ChannelManager:
    """
    Manages chat channels and coordinates message routing.

    Responsibilities:
    - Initialize enabled channels (Telegram, WhatsApp, etc.)
    - Start/stop channels
    - Route outbound messages
    """

    def __init__(
        self,
        config: Config,
        bus: MessageBus,
        *,
        session_manager: "SessionManager | None" = None,
        webui_runtime_model_name: Callable[[], str | None] | None = None,
        webui_runtime_model_preset: Callable[[], str | None] | None = None,
        webui_runtime_concurrency_snapshot: Callable[[], dict] | None = None,
        cron_service: "CronService | None" = None,
    ):
        self.config = config
        self.bus = bus
        self._session_manager = session_manager
        self._webui_runtime_model_name = webui_runtime_model_name
        self._webui_runtime_model_preset = webui_runtime_model_preset
        self._webui_runtime_concurrency_snapshot = webui_runtime_concurrency_snapshot
        # Running CronService instance (same gateway process) — handed to the
        # websocket channel so its run-now endpoint reaches the live scheduler
        # (and its in-process overlap guard), not a fresh action-log copy.
        self._cron_service = cron_service
        self.channels: dict[str, BaseChannel] = {}
        if bus is not None:
            self.bus.set_inbound_authorizer(self._authorize_inbound)
        self._dispatch_task: asyncio.Task | None = None
        self._origin_reply_fingerprints: dict[tuple[str, str, str], str] = {}
        # Fire-and-forget tasks (e.g. the restart-completion notice) are
        # parked here so the event loop keeps a strong reference and does
        # not garbage-collect them mid-flight. Same pattern the channels
        # use (see dingtalk._background_tasks).
        self._background_tasks: set[asyncio.Task] = set()

        # Shared transcription service — built once from the global
        # config and injected into every channel so the backend transcribes
        # audio before it reaches the agent loop. Channel-level
        # ``transcription_*`` attributes remain as a legacy fallback for any
        # channel constructed outside this manager.
        from durin.service.transcription import TranscriptionService

        self.transcription = TranscriptionService.from_config(config.transcription)

        from durin.service.speech import SpeechSynthesisService

        self.speech_synthesis = SpeechSynthesisService.from_config(config.tts)

        self._init_channels()

    def _ensure_channel_extras(self) -> None:
        """Install the pip extra for any enabled channel whose dep is missing, so
        the next gateway start can load it. A freshly-installed channel SDK needs a
        restart — its module-level import (slack) or availability flag (discord) was
        already evaluated — so ``ensure_or_note`` logs a restart note."""
        from durin.extras import REGISTRY, _module_present, ensure_or_note

        for name in ("slack", "discord"):
            section = getattr(self.config.channels, name, None)
            if section is None:
                continue
            enabled = (
                section.get("enabled", False)
                if isinstance(section, dict)
                else getattr(section, "enabled", False)
            )
            fe = REGISTRY.get(name)
            if enabled and fe is not None and not _module_present(fe.module):
                ensure_or_note(name, config=self.config)

    def _make_channel(self, name: str) -> "BaseChannel | None":
        """Instantiate one channel by name from the current config.

        Reads the channel section fresh from ``self.config``, resolves secret
        refs, applies per-channel overrides (transcription, bool flags), and
        returns the new instance — or ``None`` if the channel class is not
        found or the section is absent.  Does NOT start the channel.
        """
        from durin.channels.registry import discover_all

        cls = discover_all().get(name)
        if cls is None:
            return None
        section = getattr(self.config.channels, name, None)
        if section is None:
            return None

        transcription_provider = self.config.channels.transcription_provider
        transcription_key = self._resolve_transcription_key(transcription_provider)
        transcription_base = self._resolve_transcription_base(transcription_provider)
        transcription_language = self.config.channels.transcription_language

        kwargs: dict[str, Any] = {}
        if cls.name == "websocket":
            if self._session_manager is not None:
                kwargs["session_manager"] = self._session_manager
                static_path = _default_webui_dist()
                if static_path is not None:
                    kwargs["static_dist_path"] = static_path
            if self._webui_runtime_model_name is not None:
                kwargs["runtime_model_name"] = self._webui_runtime_model_name
            if self._webui_runtime_model_preset is not None:
                kwargs["runtime_model_preset"] = self._webui_runtime_model_preset
            if self._webui_runtime_concurrency_snapshot is not None:
                kwargs["runtime_concurrency_snapshot"] = self._webui_runtime_concurrency_snapshot
            if self._cron_service is not None:
                kwargs["cron_service"] = self._cron_service

        channel = cls(_resolve_section_secrets(section), self.bus, **kwargs)
        channel.transcription_provider = transcription_provider
        channel.transcription_api_key = transcription_key
        channel.transcription_api_base = transcription_base
        channel.transcription_language = transcription_language
        shared_transcription = getattr(self, "transcription", None)
        if shared_transcription is not None:
            channel.transcription = shared_transcription
        channel.speech_synthesis = getattr(self, "speech_synthesis", None)
        channel.voice_config = getattr(self.config, "voice", None)
        channel.send_progress = self._resolve_bool_override(
            section, "send_progress", self.config.channels.send_progress,
        )
        channel.send_tool_hints = self._resolve_bool_override(
            section, "send_tool_hints", self.config.channels.send_tool_hints,
        )
        channel.show_reasoning = self._resolve_bool_override(
            section, "show_reasoning", self.config.channels.show_reasoning,
        )
        return channel

    def _init_channels(self) -> None:
        """Initialize channels discovered via pkgutil scan + entry_points plugins."""
        self._ensure_channel_extras()
        from durin.channels.registry import discover_all

        for name, cls in discover_all().items():
            section = getattr(self.config.channels, name, None)
            if section is None:
                continue
            enabled = (
                section.get("enabled", False)
                if isinstance(section, dict)
                else getattr(section, "enabled", False)
            )
            if not enabled:
                continue
            try:
                channel = self._make_channel(name)
                if channel is None:
                    continue
                self.channels[name] = channel
                logger.info("{} channel enabled", cls.display_name)
            except Exception as e:
                logger.warning("{} channel not available: {}", name, e)

        self._validate_allow_from()

    def _resolve_transcription_key(self, provider: str) -> str:
        """Pick the API key for the configured transcription provider, resolving
        any ``${secret:}`` reference — base.py hands this straight to the Whisper
        provider, so an unresolved ref would fail voice transcription auth."""
        from durin.security.secrets import resolve_secret
        try:
            if provider == "openai":
                return resolve_secret(self.config.providers.openai.api_key)
            return resolve_secret(self.config.providers.groq.api_key)
        except AttributeError:
            return ""

    def _resolve_transcription_base(self, provider: str) -> str:
        """Pick the API base URL for the configured transcription provider."""
        try:
            if provider == "openai":
                return self.config.providers.openai.api_base or ""
            return self.config.providers.groq.api_base or ""
        except AttributeError:
            return ""

    async def _authorize_inbound(self, msg: InboundMessage) -> bool:
        """Central inbound-authorization gate, installed on the bus at init time.

        Unknown channels (cli, cron, subagent, TUI) are always trusted — they
        are internal and never arrive over an external chat transport.  Known
        channels delegate to ``channel.is_allowed``; unauthorized DM senders
        receive a pairing code, unauthorized group messages are silently denied.
        """
        channel = self.channels.get(msg.channel)
        if channel is None:
            return True  # internal / non-chat origin — never block
        if channel.is_allowed(msg.sender_id):
            return True
        if msg.is_dm:
            code = generate_code(channel.name, str(msg.sender_id))
            await self._send_with_retry(channel, OutboundMessage(
                channel=channel.name,
                chat_id=str(msg.chat_id),
                content=format_pairing_reply(code),
                metadata={PAIRING_CODE_META_KEY: code},
            ))
            logger.info("Sent pairing code {} to {} on {}", code, msg.sender_id, channel.name)
        else:
            logger.warning("Access denied for {} on {}", msg.sender_id, channel.name)
        return False

    def _validate_allow_from(self) -> None:
        for name, ch in self.channels.items():
            cfg = ch.config
            if isinstance(cfg, dict):
                if "allow_from" in cfg:
                    allow = cfg.get("allow_from")
                else:
                    allow = cfg.get("allowFrom")
            else:
                allow = getattr(cfg, "allow_from", None)
            if allow is None:
                # allowFrom omitted → pairing-only mode.  Unapproved senders
                # receive a pairing code instead of being silently ignored.
                logger.info(
                    '"{}" has no allowFrom; unapproved users will receive a pairing code',
                    name,
                )

    def _should_send_progress(self, channel_name: str, *, tool_hint: bool = False) -> bool:
        """Return whether progress (or tool-hints) may be sent to *channel_name*."""
        ch = self.channels.get(channel_name)
        if ch is None:
            logger.warning("Progress check for unknown channel: {}", channel_name)
            return False
        if tool_hint:
            # ``send_tool_hints`` gates chat-TEXT hints. Channels that render
            # structured tool payloads (webui panels, plan cards) depend on
            # the START frame arriving while the tool runs — a blocking
            # ask_user would otherwise show nothing to answer
            # (durin/agent/user_payloads.py).
            from durin.agent.user_payloads import channel_renders_tool_payloads

            if channel_renders_tool_payloads(channel_name):
                return True
            return ch.send_tool_hints
        return ch.send_progress

    def _resolve_bool_override(self, section: Any, key: str, default: bool) -> bool:
        """Return *key* from *section* if it is a bool, otherwise *default*.

        For dict configs also checks the camelCase alias (e.g. ``sendProgress``
        for ``send_progress``) so raw JSON/TOML configs work alongside
        Pydantic models.
        """
        if isinstance(section, dict):
            value = section.get(key)
            if value is None:
                camel = _BOOL_CAMEL_ALIASES.get(key)
                if camel:
                    value = section.get(camel)
            return value if isinstance(value, bool) else default
        value = getattr(section, key, None)
        return value if isinstance(value, bool) else default

    async def _start_channel(self, name: str, channel: BaseChannel) -> None:
        """Run a channel under crash supervision.

        A clean return from ``channel.start()`` ends supervision — that is the
        channel's signal for a deliberate stop or a fatal configuration error
        (bad token, missing privileged intents) where a restart loop would
        just hammer the platform. Any exception is treated as a transient
        crash: restart with exponential backoff, reset after stable uptime.
        """
        attempt = 0
        while True:
            started_at = time.monotonic()
            try:
                await channel.start()
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Channel {} crashed", name)
            if time.monotonic() - started_at >= _CHANNEL_STABLE_UPTIME_S:
                attempt = 0
            delay = min(_CHANNEL_RESTART_MAX_DELAY_S, float(2**attempt))
            attempt += 1
            logger.info("Restarting channel {} in {}s", name, delay)
            await asyncio.sleep(delay)

    async def start_all(self) -> None:
        """Start all channels and the outbound dispatcher."""
        if not self.channels:
            logger.warning("No channels enabled")
            return

        # Start outbound dispatcher
        self._dispatch_task = asyncio.create_task(self._dispatch_outbound())

        # Start channels
        tasks = []
        for name, channel in self.channels.items():
            logger.info("Starting {} channel...", name)
            tasks.append(asyncio.create_task(self._start_channel(name, channel)))

        self._notify_restart_done_if_needed()

        # Pre-load STT/TTS engines in the background so the first transcription /
        # voice synth doesn't pay the model load (and first-install download)
        # inline. Parked so the loop keeps a strong ref; never blocks startup.
        # ``getattr`` so managers built via ``__new__`` in tests (no __init__,
        # hence no ``_background_tasks``) still run start_all without crashing.
        park = getattr(self, "_background_tasks", None)
        if park is not None:
            warm = asyncio.create_task(self._warmup_speech())
            park.add(warm)
            warm.add_done_callback(park.discard)

        # Wait for all to complete (they should run forever)
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _warmup_speech(self) -> None:
        """Warm the shared STT/TTS services at startup for every *enabled*
        subsystem. When an enabled local subsystem's extra is not installed, the
        engine is downloaded first (gated by config.install.auto_install_extras)
        rather than deferring the install to first use; if that install is
        disabled or fails, warmup is skipped. Cloud providers warm to a no-op.
        Failures are logged, never fatal."""
        from durin.extras import _module_present, ensure_or_note

        async def _warm(svc, cfg, feature: str, module: str, label: str) -> None:
            if svc is None or not getattr(cfg, "enabled", False):
                return
            # Only a local engine needs its extra; cloud providers no-op.
            if getattr(cfg, "provider", None) == "local" and not _module_present(module):
                # Enabled but not installed: download it now (subprocess + model
                # download, so off-thread). ensure_or_note honours
                # config.install.auto_install_extras and is a fast no-op once
                # present. Skip warmup if it could not be made importable.
                res = await asyncio.to_thread(ensure_or_note, feature, config=self.config)
                if res.status not in ("present", "installed") or not _module_present(module):
                    return
            try:
                await svc.warmup()
                logger.info("{} engine warmed", label)
            except Exception as e:  # noqa: BLE001
                logger.warning("{} warmup skipped: {}", label, e)

        await _warm(
            getattr(self, "transcription", None),
            getattr(self.config, "transcription", None), "stt", "sherpa_onnx", "Transcription",
        )
        await _warm(
            getattr(self, "speech_synthesis", None),
            getattr(self.config, "tts", None), "tts", "supertonic", "Speech synthesis",
        )

    def _notify_restart_done_if_needed(self) -> None:
        """Send restart completion message when runtime env markers are present."""
        notice = consume_restart_notice_from_env()
        if not notice:
            return
        target = self.channels.get(notice.channel)
        if not target:
            return
        task = asyncio.create_task(self._send_with_retry(
            target,
            OutboundMessage(
                channel=notice.channel,
                chat_id=notice.chat_id,
                content=format_restart_completed_message(notice.started_at_raw),
                metadata=dict(notice.metadata or {}),
            ),
        ))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def stop_all(self) -> None:
        """Stop all channels and the dispatcher."""
        logger.info("Stopping all channels...")

        # Stop dispatcher
        if self._dispatch_task:
            self._dispatch_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._dispatch_task

        # Stop all channels
        for name, channel in self.channels.items():
            try:
                await channel.stop()
                logger.info("Stopped {} channel", name)
            except Exception:
                logger.exception("Error stopping {}", name)

    async def start_channel(self, name: str) -> None:
        """Hot-start a single channel without restarting the gateway.

        If the channel is already alive this is a no-op so callers can be
        idempotent.  Otherwise the config is reloaded first so a just-enabled
        section is visible, the channel is instantiated via ``_make_channel``,
        stored, and started.

        Raises on failure so the HTTP caller can surface the error.
        """
        existing = self.channels.get(name)
        if existing is not None:
            if existing.is_running:
                logger.info("Channel {} already running, skipping start", name)
                return
            # Registered but not alive: its start() bailed (e.g. the channel
            # was enabled before credentials existed) or its task ended. A
            # dict-presence check here made hot-start a permanent no-op for
            # such channels — tear it down and rebuild with fresh config.
            logger.info("Channel {} is registered but not running; restarting it", name)
            await self.stop_channel(name)
        from durin.config.loader import load_config
        self.config = load_config()
        section = getattr(self.config.channels, name, None)
        enabled = (
            (section.get("enabled", False)
             if isinstance(section, dict)
             else getattr(section, "enabled", False))
            if section is not None else False
        )
        if not enabled:
            raise ValueError(f"Channel {name} is not enabled in config; refusing to hot-start")
        channel = self._make_channel(name)
        if channel is None:
            raise ValueError(f"Unknown or unconfigured channel: {name}")
        self.channels[name] = channel
        # Some channels' start() returns once background machinery is up
        # (Telegram); others run their keepalive loop inside start() forever
        # (Slack). Awaiting the latter inline would hang the HTTP caller, so
        # give start() a short window to fail fast, then park it.
        task = asyncio.create_task(channel.start())
        done, _pending = await asyncio.wait({task}, timeout=_HOT_START_FAIL_FAST_WINDOW_S)
        if done:
            exc = task.exception()
            if exc is not None:
                del self.channels[name]
                logger.exception("Failed to hot-start channel {}", name)
                raise exc
            if not channel.is_running:
                # start() returned immediately without coming up — e.g. it
                # logged missing credentials and bailed. Surface that instead
                # of reporting a phantom success.
                del self.channels[name]
                raise ValueError(
                    f"Channel {name} did not start (check its credentials in the logs)"
                )
        else:
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
        logger.info("Hot-started channel {}", name)

    async def stop_channel(self, name: str) -> None:
        """Hot-stop a single channel without restarting the gateway.

        If the channel is not running this is a no-op.  On success the channel
        is removed from ``self.channels`` so a subsequent ``start_channel``
        re-instantiates it with fresh config.

        Exceptions from ``channel.stop()`` are logged but the channel is still
        removed from the registry (mirroring ``stop_all`` behaviour).
        """
        channel = self.channels.get(name)
        if channel is None:
            logger.info("Channel {} not running, skipping stop", name)
            return
        try:
            await channel.stop()
            logger.info("Hot-stopped channel {}", name)
        except Exception:
            logger.exception("Error stopping channel {}", name)
        finally:
            self.channels.pop(name, None)

    @staticmethod
    def _fingerprint_content(content: str) -> str:
        normalized = " ".join(content.split())
        return hashlib.sha1(normalized.encode("utf-8")).hexdigest() if normalized else ""

    def _should_suppress_outbound(self, msg: OutboundMessage) -> bool:
        metadata = msg.metadata or {}
        if metadata.get("_progress"):
            return False
        fingerprint = self._fingerprint_content(msg.content)
        if not fingerprint:
            return False

        origin_message_id = metadata.get("origin_message_id")
        if isinstance(origin_message_id, str) and origin_message_id:
            key = (msg.channel, msg.chat_id, origin_message_id)
            if self._origin_reply_fingerprints.get(key) == fingerprint:
                return True
            self._origin_reply_fingerprints[key] = fingerprint

        message_id = metadata.get("message_id")
        if isinstance(message_id, str) and message_id:
            key = (msg.channel, msg.chat_id, message_id)
            self._origin_reply_fingerprints[key] = fingerprint

        return False

    async def _dispatch_outbound(self) -> None:
        """Dispatch outbound messages to the appropriate channel."""
        logger.info("Outbound dispatcher started")

        # Buffer for messages that couldn't be processed during delta coalescing
        # (since asyncio.Queue doesn't support push_front)
        pending: list[OutboundMessage] = []

        while True:
            try:
                # First check pending buffer before waiting on queue
                if pending:
                    msg = pending.pop(0)
                else:
                    msg = await asyncio.wait_for(
                        self.bus.consume_outbound(),
                        timeout=1.0
                    )

                if (
                    msg.metadata.get("_reasoning_delta")
                    or msg.metadata.get("_reasoning_end")
                    or msg.metadata.get("_reasoning")
                ):
                    # Reasoning rides its own plugin channel: only delivered
                    # when the destination channel opts in via ``show_reasoning``
                    # and overrides the streaming primitives. Channels without
                    # a low-emphasis UI affordance keep the base no-op and the
                    # content silently drops here. ``_reasoning`` (one-shot)
                    # is accepted for backward compatibility with hooks that
                    # haven't migrated to delta/end yet.
                    channel = self.channels.get(msg.channel)
                    if channel is not None and channel.show_reasoning:
                        await self._send_with_retry(channel, msg)
                    continue

                if msg.metadata.get("_progress"):
                    if msg.metadata.get("_tool_hint") and not self._should_send_progress(
                        msg.channel, tool_hint=True,
                    ):
                        continue
                    if not msg.metadata.get("_tool_hint") and not self._should_send_progress(
                        msg.channel, tool_hint=False,
                    ):
                        continue

                if msg.metadata.get("_retry_wait"):
                    # WebUI users have no other signal that the model is being
                    # retried — surface it as a dedicated channel event there.
                    # CLI/TUI keep the historical silence; the spinner already
                    # tells the user "still working" and a retry bubble would
                    # be noise.
                    if msg.channel == "websocket":
                        channel = self.channels.get(msg.channel)
                        if channel is not None:
                            await self._send_with_retry(channel, msg)
                    continue

                if (
                    msg.metadata.get("_runtime_model_updated")
                    and msg.channel == "websocket"
                    and "websocket" not in self.channels
                ):
                    continue

                # Coalesce consecutive _stream_delta messages for the same (channel, chat_id)
                # to reduce API calls and improve streaming latency
                if msg.metadata.get("_stream_delta") and not msg.metadata.get("_stream_end"):
                    msg, extra_pending = self._coalesce_stream_deltas(msg)
                    pending.extend(extra_pending)

                channel = self.channels.get(msg.channel)
                if channel:
                    # Duplicate suppression is scoped to a known source message
                    # so repeated content from separate turns is still delivered.
                    if (
                        not msg.metadata.get("_stream_delta")
                        and not msg.metadata.get("_stream_end")
                        and not msg.metadata.get("_streamed")
                    ):
                        if self._should_suppress_outbound(msg):
                            logger.info("Suppressing duplicate outbound message to {}:{}", msg.channel, msg.chat_id)
                            continue
                    await self._send_with_retry(channel, msg)
                else:
                    logger.warning("Unknown channel: {}", msg.channel)

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

    @staticmethod
    async def _send_once(channel: BaseChannel, msg: OutboundMessage) -> None:
        """Send one outbound message without retry policy."""
        if msg.metadata.get("_reasoning_end"):
            await channel.send_reasoning_end(msg.chat_id, msg.metadata)
        elif msg.metadata.get("_reasoning_delta"):
            await channel.send_reasoning_delta(msg.chat_id, msg.content, msg.metadata)
        elif msg.metadata.get("_reasoning"):
            # Back-compat: one-shot reasoning. BaseChannel translates this
            # to a single delta + end pair so plugins only implement the
            # streaming primitives.
            await channel.send_reasoning(msg)
        elif msg.metadata.get("_stream_delta") or msg.metadata.get("_stream_end"):
            await channel.send_delta(msg.chat_id, msg.content, msg.metadata)
        elif not msg.metadata.get("_streamed"):
            await channel.send(msg)

    def _coalesce_stream_deltas(
        self, first_msg: OutboundMessage
    ) -> tuple[OutboundMessage, list[OutboundMessage]]:
        """Merge consecutive _stream_delta messages for the same (channel, chat_id).

        This reduces the number of API calls when the queue has accumulated multiple
        deltas, which happens when LLM generates faster than the channel can process.

        Returns:
            tuple of (merged_message, list_of_non_matching_messages)
        """
        target_key = (first_msg.channel, first_msg.chat_id)
        # Guard against cross-stream bleed for concurrent same-(channel, chat_id) streams
        # (e.g. Telegram forum topics): only merge messages from the same stream_id.
        target_stream_id = (first_msg.metadata or {}).get("_stream_id")
        combined_content = first_msg.content
        final_metadata = dict(first_msg.metadata or {})
        non_matching: list[OutboundMessage] = []

        # Only merge consecutive deltas. As soon as we hit any other message,
        # stop and hand that boundary back to the dispatcher via `pending`.
        while True:
            try:
                next_msg = self.bus.outbound.get_nowait()
            except asyncio.QueueEmpty:
                break

            # Check if this message belongs to the same stream
            same_target = (next_msg.channel, next_msg.chat_id) == target_key
            is_delta = next_msg.metadata and next_msg.metadata.get("_stream_delta")
            is_end = next_msg.metadata and next_msg.metadata.get("_stream_end")
            next_stream_id = (next_msg.metadata or {}).get("_stream_id")
            same_stream_id = next_stream_id == target_stream_id

            if same_target and is_delta and same_stream_id and not final_metadata.get("_stream_end"):
                # Accumulate content
                combined_content += next_msg.content
                # If we see _stream_end, remember it and stop coalescing this stream
                if is_end:
                    final_metadata["_stream_end"] = True
                    # Stream ended - stop coalescing this stream
                    break
            else:
                # First non-matching message defines the coalescing boundary.
                non_matching.append(next_msg)
                break

        merged = OutboundMessage(
            channel=first_msg.channel,
            chat_id=first_msg.chat_id,
            content=combined_content,
            metadata=final_metadata,
        )
        return merged, non_matching

    async def _send_with_retry(self, channel: BaseChannel, msg: OutboundMessage) -> None:
        """Send a message with retry on failure using exponential backoff.

        Note: CancelledError is re-raised to allow graceful shutdown.
        """
        max_attempts = max(self.config.channels.send_max_retries, 1)

        for attempt in range(max_attempts):
            try:
                await self._send_once(channel, msg)
                return  # Send succeeded
            except asyncio.CancelledError:
                raise  # Propagate cancellation for graceful shutdown
            except Exception as e:
                if attempt == max_attempts - 1:
                    logger.exception(
                        "Failed to send to {} after {} attempts",
                        msg.channel, max_attempts
                    )
                    return
                delay = _SEND_RETRY_DELAYS[min(attempt, len(_SEND_RETRY_DELAYS) - 1)]
                logger.warning(
                    "Send to {} failed (attempt {}/{}): {}, retrying in {}s",
                    msg.channel, attempt + 1, max_attempts, type(e).__name__, delay
                )
                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    raise  # Propagate cancellation during sleep

    def get_channel(self, name: str) -> BaseChannel | None:
        """Get a channel by name."""
        return self.channels.get(name)

    def get_status(self) -> dict[str, Any]:
        """Get status of all channels."""
        return {
            name: {
                "enabled": True,
                "running": channel.is_running
            }
            for name, channel in self.channels.items()
        }

    @property
    def enabled_channels(self) -> list[str]:
        """Get list of enabled channel names."""
        return list(self.channels.keys())
