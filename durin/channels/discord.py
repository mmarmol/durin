"""Discord channel implementation using discord.py."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import os
import tempfile
import time
from collections import OrderedDict
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

from loguru import logger
from pydantic import Field

from durin.bus.events import OutboundMessage
from durin.bus.queue import MessageBus
from durin.channels.base import BaseChannel
from durin.command.builtin import build_help_text, specs_for_surface
from durin.config.paths import get_media_dir
from durin.config.schema import Base
from durin.utils.helpers import convert_gfm_tables, safe_filename, split_message

DISCORD_AVAILABLE = importlib.util.find_spec("discord") is not None
if TYPE_CHECKING:
    import aiohttp
    import discord
    from discord import app_commands
    from discord.abc import Messageable

if DISCORD_AVAILABLE:
    import discord
    from discord import app_commands
    from discord.abc import Messageable

    def _gateway_intents() -> "discord.Intents":
        """The gateway subscriptions this adapter's handlers require.

        Derived rather than configured: every bit below is demanded by an event
        the channel actually implements, and durin implements no member,
        presence, voice or reaction-event handler, so asking for those would be
        a privileged request nothing consumes.  Reactions the bot *adds* are
        REST calls and need no intent.
        """
        intents = discord.Intents.none()
        intents.guilds = True  # on_thread_update / on_thread_delete, channel cache
        intents.guild_messages = True  # on_message in servers
        intents.dm_messages = True  # on_message in direct messages
        intents.message_content = True  # privileged: read the text of a message
        return intents

    GATEWAY_INTENTS = _gateway_intents()

MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024  # 20MB
MAX_MESSAGE_LEN = 2000  # Discord message character limit
TYPING_INTERVAL_S = 8
LIVENESS_INTERVAL_S = 60.0
LIVENESS_TIMEOUT_S = 15.0
LIVENESS_MAX_FAILURES = 3
_DEDUP_TTL_S = 300.0
_DEDUP_MAX_IDS = 5000
_SPLIT_SUSPECT_LEN = 1900
_SPLIT_FLUSH_DELAY_S = 2.0


@dataclass
class _StreamBuf:
    """Per-chat streaming accumulator for progressive Discord message edits."""

    text: str = ""
    message: Any | None = None
    last_edit: float = 0.0
    stream_id: str | None = None


@dataclass
class _SplitBuf:
    """Reassembly buffer for messages the Discord client split at 2000 chars."""

    message: Any
    parts: list[str]
    task: asyncio.Task[None] | None = None


class DiscordConfig(Base):
    """Discord channel configuration.

    Sender authorization (who may talk to durin) lives in ``allow_from`` and is
    enforced by the central bus-ingress gate; unapproved DM senders receive a
    pairing code.  ``allow_channels`` is routing, not auth: an empty list means
    every channel the bot can see, and a non-empty list is a closed allowlist.

    Gateway intents are not configurable: they are derived from the events this
    adapter handles (see ``GATEWAY_INTENTS``).  A raw bitfield had no safe home
    — the setup flow cannot verify it, a form cannot render it, and a mistyped
    digit yields a bot that connects and silently ignores every message.
    """

    enabled: bool = False
    token: str = Field(default="", json_schema_extra={"secret": True, "required": True})
    allow_from: list[str] = Field(default_factory=list, json_schema_extra={"group": "access"})
    # Empty = every visible channel; non-empty = closed allowlist (a thread is
    # matched by its parent channel too, see DiscordChannel._channel_allow_keys).
    allow_channels: list[str] = Field(
        default_factory=list, json_schema_extra={"group": "access"}
    )
    group_policy: Literal["mention", "open"] = Field(
        default="mention", json_schema_extra={"group": "access"}
    )
    read_receipt_emoji: str = Field(default="👀", json_schema_extra={"group": "behavior"})
    working_emoji: str = Field(default="🔧", json_schema_extra={"group": "behavior"})
    working_emoji_delay: float = Field(default=2.0, json_schema_extra={"group": "behavior"})
    streaming: bool = Field(default=True, json_schema_extra={"group": "behavior"})
    proxy: str | None = Field(default=None, json_schema_extra={"group": "security"})
    proxy_username: str | None = Field(default=None, json_schema_extra={"group": "security"})
    proxy_password: str | None = Field(
        default=None, json_schema_extra={"secret": True, "group": "security"}
    )


if DISCORD_AVAILABLE:

    class DiscordBotClient(discord.Client):
        """discord.py client that forwards events to the channel."""

        def __init__(
            self,
            channel: DiscordChannel,
            *,
            intents: discord.Intents,
            proxy: str | None = None,
            proxy_auth: aiohttp.BasicAuth | None = None,
        ) -> None:
            super().__init__(
                intents=intents,
                proxy=proxy,
                proxy_auth=proxy_auth,
                # LLM output is untrusted for mention purposes: echoed
                # @everyone/@here or role pings must never fan out to a server.
                # Users may still be mentioned (replies, direct address).
                allowed_mentions=discord.AllowedMentions(
                    everyone=False, roles=False, users=True, replied_user=False
                ),
            )
            self._channel = channel
            self.tree = app_commands.CommandTree(self)
            self._register_app_commands()

        async def on_ready(self) -> None:
            self._channel._bot_user_id = str(self.user.id) if self.user else None
            self._channel.logger.info("bot connected as user {}", self._channel._bot_user_id)
            self._channel._start_liveness_probe()
            if not self._channel._commands_synced:
                # Global command sync is daily-rate-limited; on_ready re-fires
                # after re-identify and the channel restarts under supervision,
                # so sync exactly once per gateway process.
                try:
                    synced = await self.tree.sync()
                    self._channel._commands_synced = True
                    self._channel.logger.info("app commands synced: {}", len(synced))
                except Exception as e:
                    self._channel.logger.warning("app command sync failed: {}", e)

        async def on_message(self, message: discord.Message) -> None:
            await self._channel._handle_discord_message(message)

        async def on_thread_delete(self, thread: discord.Thread) -> None:
            self._channel._forget_channel(thread)

        async def on_thread_update(self, before: discord.Thread, after: discord.Thread) -> None:
            if getattr(after, "archived", False):
                self._channel._forget_channel(after)
            else:
                self._channel._remember_channel(after)

        async def _reply_ephemeral(self, interaction: discord.Interaction, text: str) -> bool:
            """Send an ephemeral interaction response and report success."""
            try:
                await interaction.response.send_message(text, ephemeral=True)
                return True
            except Exception as e:
                self._channel.logger.warning("interaction response failed: {}", e)
                return False

        async def _resolve_interaction_channel(
            self,
            interaction: discord.Interaction,
        ) -> Any | None:
            channel_id = interaction.channel_id
            if channel_id is None:
                return None
            channel = getattr(interaction, "channel", None) or self.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await self.fetch_channel(channel_id)
                except Exception as e:
                    self._channel.logger.warning("interaction channel {} unavailable: {}", channel_id, e)
                    return None
            self._channel._remember_channel(channel)
            return channel

        async def _interaction_channel_allowed(
            self,
            interaction: discord.Interaction,
            channel: Any | None,
        ) -> bool:
            allow_channels = self._channel.config.allow_channels
            if not allow_channels:
                return True
            if channel is None:
                channel_id = interaction.channel_id
                return channel_id is not None and str(channel_id) in allow_channels
            channel_ids = self._channel._channel_allow_keys(channel)
            return not channel_ids.isdisjoint(allow_channels)

        async def _forward_slash_command(
            self,
            interaction: discord.Interaction,
            command_text: str,
        ) -> None:
            sender_id = str(interaction.user.id)
            channel_id = interaction.channel_id

            if channel_id is None:
                self._channel.logger.warning("slash command missing channel_id: {}", command_text)
                return

            if not self._channel.is_allowed(sender_id):
                await self._reply_ephemeral(interaction, "You are not allowed to use this bot.")
                return

            channel = await self._resolve_interaction_channel(interaction)
            if not await self._interaction_channel_allowed(interaction, channel):
                await self._reply_ephemeral(interaction, "This channel is not allowed for this bot.")
                return

            await self._reply_ephemeral(interaction, f"Processing {command_text}...")

            metadata: dict[str, Any] = {
                "interaction_id": str(interaction.id),
                "guild_id": str(interaction.guild_id) if interaction.guild_id else None,
                "is_slash_command": True,
            }
            session_key = None
            if channel is not None:
                parent_channel_id = self._channel._channel_parent_key(channel)
                if parent_channel_id is not None:
                    metadata["parent_channel_id"] = parent_channel_id
                    metadata["context_chat_id"] = parent_channel_id
                    metadata["thread_id"] = str(channel_id)
                    session_key = f"{self._channel.name}:{parent_channel_id}:thread:{channel_id}"

            await self._channel._handle_message(
                sender_id=sender_id,
                chat_id=str(channel_id),
                content=command_text,
                metadata=metadata,
                session_key=session_key,
            )

        def _register_app_commands(self) -> None:
            for spec in specs_for_surface("channels"):
                name = spec.command.lstrip("/").replace("-", "_")
                if name == "help":
                    continue  # registered below with a local, ephemeral reply
                description = spec.title[:100]

                @self.tree.command(name=name, description=description)
                async def command_handler(
                    interaction: discord.Interaction,
                    _command_text: str = spec.command,
                ) -> None:
                    await self._forward_slash_command(interaction, _command_text)

            @self.tree.command(name="help", description="Show available commands")
            async def help_command(interaction: discord.Interaction) -> None:
                sender_id = str(interaction.user.id)
                if not self._channel.is_allowed(sender_id):
                    await self._reply_ephemeral(interaction, "You are not allowed to use this bot.")
                    return
                channel = await self._resolve_interaction_channel(interaction)
                if not await self._interaction_channel_allowed(interaction, channel):
                    await self._reply_ephemeral(interaction, "This channel is not allowed for this bot.")
                    return
                await self._reply_ephemeral(interaction, build_help_text("channels"))

            @self.tree.error
            async def on_app_command_error(
                interaction: discord.Interaction,
                error: app_commands.AppCommandError,
            ) -> None:
                command_name = interaction.command.qualified_name if interaction.command else "?"
                self._channel.logger.warning(
                    "app command failed user={} channel={} cmd={} error={}",
                    interaction.user.id,
                    interaction.channel_id,
                    command_name,
                    error,
                )

        async def send_outbound(self, msg: OutboundMessage) -> None:
            """Send a durin outbound message using Discord transport rules."""
            channel_id = int(msg.chat_id)

            channel = self._channel._known_channels.get(msg.chat_id) or self.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await self.fetch_channel(channel_id)
                except Exception as e:
                    self._channel.logger.warning("channel {} unavailable: {}", msg.chat_id, e)
                    return

            forum_types = {discord.ChannelType.forum, discord.ChannelType.media}
            if getattr(channel, "type", None) in forum_types:
                await self._send_to_forum(channel, msg)
                return

            reference, mention_settings = self._build_reply_context(channel, msg.reply_to)
            sent_media = False
            failed_media: list[str] = []

            for index, media_path in enumerate(msg.media or []):
                if await self._send_file(
                    channel,
                    media_path,
                    reference=reference if index == 0 else None,
                    mention_settings=mention_settings,
                ):
                    sent_media = True
                else:
                    failed_media.append(Path(media_path).name)

            for index, chunk in enumerate(
                self._build_chunks(msg.content or "", failed_media, sent_media)
            ):
                kwargs: dict[str, Any] = {"content": chunk}
                if index == 0 and reference is not None and not sent_media:
                    kwargs["reference"] = reference
                    kwargs["allowed_mentions"] = mention_settings
                await channel.send(**kwargs)

        async def _send_to_forum(self, forum: Any, msg: OutboundMessage) -> None:
            """Forum/media channels reject plain sends; post a new thread instead."""
            files: list[discord.File] = []
            failed_media: list[str] = []
            for media_path in msg.media or []:
                path = Path(media_path)
                if path.is_file():
                    files.append(discord.File(path))
                else:
                    failed_media.append(path.name)
            chunks = self._build_chunks(msg.content or "", failed_media, bool(files)) or ["…"]
            first_line = (msg.content or "").strip().splitlines()[0] if (msg.content or "").strip() else ""
            name = (first_line or "durin")[:100]
            kwargs: dict[str, Any] = {"name": name, "content": chunks[0]}
            if files:
                kwargs["files"] = files
            created = await forum.create_thread(**kwargs)
            thread = getattr(created, "thread", created)
            self._channel._remember_channel(thread)
            for chunk in chunks[1:]:
                await thread.send(content=chunk)

        async def _send_file(
            self,
            channel: Messageable,
            file_path: str,
            *,
            reference: discord.PartialMessage | None,
            mention_settings: discord.AllowedMentions,
        ) -> bool:
            """Send a file attachment via discord.py."""
            path = Path(file_path)
            if not path.is_file():
                self._channel.logger.warning("file not found, skipping: {}", file_path)
                return False

            if path.stat().st_size > MAX_ATTACHMENT_BYTES:
                self._channel.logger.warning("file too large (>20MB), skipping: {}", path.name)
                return False

            try:
                kwargs: dict[str, Any] = {"file": discord.File(path)}
                if reference is not None:
                    kwargs["reference"] = reference
                    kwargs["allowed_mentions"] = mention_settings
                await channel.send(**kwargs)
                self._channel.logger.info("file sent: {}", path.name)
                return True
            except Exception:
                self._channel.logger.exception("Error sending file {}", path.name)
                return False

        @staticmethod
        def _build_chunks(content: str, failed_media: list[str], sent_media: bool) -> list[str]:
            """Build outbound text chunks, including attachment-failure fallback text."""
            content = convert_gfm_tables(content)
            chunks = split_message(content, MAX_MESSAGE_LEN)
            if chunks or not failed_media or sent_media:
                return chunks
            fallback = "\n".join(f"[attachment: {name} - send failed]" for name in failed_media)
            return split_message(fallback, MAX_MESSAGE_LEN)

        def _build_reply_context(
            self,
            channel: Messageable,
            reply_to: str | None,
        ) -> tuple[discord.PartialMessage | None, discord.AllowedMentions]:
            """Build reply context for outbound messages."""
            mention_settings = discord.AllowedMentions(replied_user=False)
            if not reply_to:
                return None, mention_settings
            try:
                message_id = int(reply_to)
            except ValueError:
                self._channel.logger.warning("Invalid reply target: {}", reply_to)
                return None, mention_settings

            return channel.get_partial_message(message_id), mention_settings


class DiscordChannel(BaseChannel):
    """Discord channel using discord.py."""

    name = "discord"
    display_name = "Discord"
    _STREAM_EDIT_INTERVAL = 0.8

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return DiscordConfig().model_dump(by_alias=False)

    @classmethod
    def config_model(cls) -> type | None:
        return DiscordConfig

    @staticmethod
    def _channel_key(channel_or_id: Any) -> str:
        """Normalize channel-like objects and ids to a stable string key."""
        channel_id = getattr(channel_or_id, "id", channel_or_id)
        return str(channel_id)

    @classmethod
    def _channel_allow_keys(cls, channel: Any) -> set[str]:
        """Return channel IDs that can satisfy allow_channels for this channel."""
        keys = {cls._channel_key(channel)}
        if parent_key := cls._channel_parent_key(channel):
            keys.add(parent_key)
        return keys

    @classmethod
    def _channel_parent_key(cls, channel: Any) -> str | None:
        """Return the parent channel key for a Discord thread-like channel."""
        parent_id = getattr(channel, "parent_id", None)
        if parent_id is not None:
            return cls._channel_key(parent_id)
        parent = getattr(channel, "parent", None)
        if parent is not None:
            return cls._channel_key(parent)
        return None

    _AUDIO_EXTENSIONS = {".ogg", ".oga", ".opus", ".mp3", ".m4a", ".wav", ".flac"}

    @classmethod
    def _is_audio_attachment(cls, attachment: discord.Attachment) -> bool:
        content_type = (getattr(attachment, "content_type", None) or "").lower()
        if content_type.startswith("audio/"):
            return True
        suffix = Path(getattr(attachment, "filename", "") or "").suffix.lower()
        return suffix in cls._AUDIO_EXTENSIONS

    def __init__(self, config: Any, bus: MessageBus):
        if isinstance(config, dict):
            if "intents" in config:
                # Pydantic drops unknown keys without a word.  Someone who once
                # hand-tuned this bitfield deserves to hear that their value no
                # longer does anything, rather than debug a bot that ignores it.
                logger.warning(
                    "channels.discord.intents is obsolete and ignored: gateway "
                    "intents are derived from the events durin handles. Remove "
                    "the key from your config."
                )
            config = DiscordConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: DiscordConfig = config
        self._client: DiscordBotClient | None = None
        self._typing_tasks: dict[str, asyncio.Task[None]] = {}
        self._bot_user_id: str | None = None
        self._pending_reactions: dict[str, Any] = {}  # chat_id -> message object
        self._working_emoji_tasks: dict[str, asyncio.Task[None]] = {}
        self._stream_bufs: dict[str, _StreamBuf] = {}
        self._pending_splits: dict[tuple[str, str], _SplitBuf] = {}
        self._known_channels: dict[str, Any] = {}
        self._stop_requested = False
        self._liveness_failed = False
        self._liveness_task: asyncio.Task[None] | None = None
        self._seen_message_ids: OrderedDict[str, float] = OrderedDict()
        self._commands_synced = False
        self._token_lock_fd: int | None = None

    def _acquire_token_lock(self) -> bool:
        """One gateway per bot token per host.

        Two clients on the same token both receive every event and reply
        twice (split-brain). An advisory flock held for the channel's
        lifetime rejects the second starter with a clear error instead.
        """
        if fcntl is None or not self.config.token:
            return True
        digest = hashlib.sha256(self.config.token.encode()).hexdigest()[:16]
        lock_path = Path(tempfile.gettempdir()) / f"durin-discord-{digest}.lock"
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._token_lock_fd = fd
        return True

    def _release_token_lock(self) -> None:
        fd, self._token_lock_fd = self._token_lock_fd, None
        if fd is not None:
            with suppress(OSError):
                os.close(fd)  # closing the fd releases the flock

    def _remember_channel(self, channel: Any) -> None:
        self._known_channels[self._channel_key(channel)] = channel

    def _forget_channel(self, channel_or_id: Any) -> None:
        self._known_channels.pop(self._channel_key(channel_or_id), None)

    async def start(self) -> None:
        """Start the Discord client."""
        if not DISCORD_AVAILABLE:
            self.logger.error("discord.py not installed. Run: pip install durin-ai[discord]")
            return

        if not self.config.token:
            self.logger.error("bot token not configured")
            return

        if not self._acquire_token_lock():
            self.logger.error(
                "another running process already uses this Discord bot token "
                "(a second client would double-reply); not starting"
            )
            return

        try:
            intents = GATEWAY_INTENTS

            proxy_auth = None
            has_user = bool(self.config.proxy_username)
            has_pass = bool(self.config.proxy_password)
            if has_user and has_pass:
                import warnings

                import aiohttp

                # discord.py's ``Client(proxy_auth=...)`` requires an
                # ``aiohttp.BasicAuth`` instance — we cannot switch to the
                # ``encode_basic_auth`` form aiohttp 4.0 will mandate without
                # breaking discord.py's API. Suppress the forced deprecation
                # warning narrowly until discord.py migrates.
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", DeprecationWarning)
                    proxy_auth = aiohttp.BasicAuth(
                        login=self.config.proxy_username,
                        password=self.config.proxy_password,
                    )
            elif has_user != has_pass:
                self.logger.warning(
                    "proxy auth incomplete: both proxy_username and "
                    "proxy_password must be set; ignoring partial credentials",
                )

            self._client = DiscordBotClient(
                self,
                intents=intents,
                proxy=self.config.proxy,
                proxy_auth=proxy_auth,
            )
        except Exception:
            self.logger.exception("Failed to initialize client")
            self._client = None
            self._running = False
            self._release_token_lock()
            return

        self._stop_requested = False
        self._liveness_failed = False
        self._running = True
        self.logger.info("Starting client via discord.py...")

        try:
            await self._client.start(self.config.token)
        except asyncio.CancelledError:
            raise
        except discord.LoginFailure:
            # Fatal config error: restarting cannot help and hammers the API.
            self.logger.error(
                "Discord rejected the bot token; fix channels.discord.token and restart"
            )
        except discord.PrivilegedIntentsRequired:
            self.logger.error(
                "Privileged intents are not enabled for this bot. Enable "
                "'Message Content Intent' on the Bot page of your application "
                "in the Discord Developer Portal; durin cannot read messages "
                "without it."
            )
        finally:
            self._running = False
            await self._reset_runtime_state(close_client=True)

        if self._liveness_failed and not self._stop_requested:
            self._liveness_failed = False
            # Zombie socket was force-closed by the liveness probe; surface it
            # as a crash so the manager supervisor reconnects us.
            raise RuntimeError("discord connection liveness lost")

    async def stop(self) -> None:
        """Stop the Discord channel."""
        self._stop_requested = True
        self._running = False
        await self._reset_runtime_state(close_client=True)

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through Discord using discord.py."""
        client = self._client
        if client is None or not client.is_ready():
            # Raise so the manager's retry policy covers reconnect windows;
            # a silent return here loses the message permanently.
            raise RuntimeError("discord client not ready")

        is_progress = bool((msg.metadata or {}).get("_progress"))

        try:
            await client.send_outbound(msg)
        except Exception:
            self.logger.exception("Error sending message")
            raise
        finally:
            if not is_progress:
                await self._stop_typing(msg.chat_id)
                await self._clear_reactions(msg.chat_id)

    async def send_delta(
        self, chat_id: str, delta: str, metadata: dict[str, Any] | None = None
    ) -> None:
        """Progressive Discord delivery: send once, then edit until the stream ends."""
        client = self._client
        if client is None or not client.is_ready():
            # Raise so the manager's retry policy covers reconnect windows;
            # a silent return here loses the message permanently.
            raise RuntimeError("discord client not ready")

        meta = metadata or {}
        stream_id = meta.get("_stream_id")

        if meta.get("_stream_end"):
            buf = self._stream_bufs.get(chat_id)
            if not buf or buf.message is None or not buf.text:
                return
            if stream_id is not None and buf.stream_id is not None and buf.stream_id != stream_id:
                return
            await self._finalize_stream(chat_id, buf)
            return

        buf = self._stream_bufs.get(chat_id)
        if buf is None or (
            stream_id is not None and buf.stream_id is not None and buf.stream_id != stream_id
        ):
            buf = _StreamBuf(stream_id=stream_id)
            self._stream_bufs[chat_id] = buf
        elif buf.stream_id is None:
            buf.stream_id = stream_id

        buf.text += delta
        if not buf.text.strip():
            return

        target = await self._resolve_channel(chat_id)
        if target is None:
            self.logger.warning("stream target {} unavailable", chat_id)
            return

        now = time.monotonic()
        if buf.message is None:
            try:
                buf.message = await target.send(content=buf.text)
                buf.last_edit = now
            except Exception as e:
                self.logger.warning("stream initial send failed: {}", e)
                raise
            return

        if (now - buf.last_edit) < self._STREAM_EDIT_INTERVAL:
            return

        try:
            await buf.message.edit(content=DiscordBotClient._build_chunks(buf.text, [], False)[0])
            buf.last_edit = now
        except Exception as e:
            self.logger.warning("stream edit failed: {}", e)
            raise

    def _is_duplicate(self, message_id: str) -> bool:
        """TTL'd replay guard: gateway RESUME re-delivers recent events."""
        now = time.monotonic()
        while self._seen_message_ids:
            oldest_id, seen_at = next(iter(self._seen_message_ids.items()))
            if now - seen_at > _DEDUP_TTL_S or len(self._seen_message_ids) >= _DEDUP_MAX_IDS:
                self._seen_message_ids.pop(oldest_id, None)
            else:
                break
        if message_id in self._seen_message_ids:
            return True
        self._seen_message_ids[message_id] = now
        return False

    async def _handle_discord_message(self, message: discord.Message) -> None:
        """Handle incoming Discord messages from discord.py.

        Self-loop guard: only drop messages from this bot's own account. Messages
        from other bots are allowed through so multi-agent setups (one bot asking
        another for help, a bot mentioning another by @name, etc.) can work.
        Bot-from-bot loops are still prevented per-instance because each bot
        still ignores its own outbound messages. (#3217)
        """
        if self._bot_user_id is not None and str(message.author.id) == self._bot_user_id:
            return
        if self._is_system_message(message):
            return
        if self._is_duplicate(str(message.id)):
            return

        sender_id = str(message.author.id)
        channel_id = self._channel_key(message.channel)
        self._remember_channel(message.channel)
        content = message.content or ""

        if not self._should_accept_inbound(message, content):
            return

        key = (channel_id, sender_id)
        buf = self._pending_splits.get(key)
        if buf is not None and not message.attachments:
            buf.parts.append(content)
            if len(content) >= _SPLIT_SUSPECT_LEN:
                self._reschedule_split_flush(key, buf)
                return
            await self._flush_split(key)
            return
        if len(content) >= _SPLIT_SUSPECT_LEN and not message.attachments:
            # Looks like the first piece of a client-side 2000-char split:
            # hold briefly for its continuation so the agent gets one turn.
            buf = _SplitBuf(message=message, parts=[content])
            self._pending_splits[key] = buf
            self._reschedule_split_flush(key, buf)
            return
        await self._dispatch_inbound(message, content)

    def _reschedule_split_flush(self, key: tuple[str, str], buf: _SplitBuf) -> None:
        if buf.task is not None and not buf.task.done():
            buf.task.cancel()

        async def _flush_later() -> None:
            await asyncio.sleep(_SPLIT_FLUSH_DELAY_S)
            try:
                await self._flush_split(key)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception("Timer flush failed for split buffer {}", key)

        buf.task = asyncio.create_task(_flush_later())

    async def _flush_split(self, key: tuple[str, str]) -> None:
        buf = self._pending_splits.pop(key, None)
        if buf is None:
            return
        # When the timer fires, _flush_later calls this method from within
        # buf.task itself, so buf.task IS the running task here. Cancelling
        # your own running task doesn't raise immediately — asyncio marks it
        # and raises CancelledError at the task's next real suspension point,
        # which would land inside _dispatch_inbound below and silently drop
        # the message. Only cancel the timer when some other caller (e.g. a
        # continuation part arriving) is flushing early.
        if buf.task is not None and buf.task is not asyncio.current_task() and not buf.task.done():
            buf.task.cancel()
        await self._dispatch_inbound(buf.message, "\n".join(buf.parts))

    async def _dispatch_inbound(self, message: discord.Message, content: str) -> None:
        sender_id = str(message.author.id)
        channel_id = self._channel_key(message.channel)

        authorized = self.is_allowed(sender_id)

        media_paths, attachment_markers = (
            (await self._download_attachments(message.attachments)) if authorized else ([], [])
        )
        full_content = self._compose_inbound_content(content, attachment_markers)
        metadata = self._build_inbound_metadata(message)
        parent_channel_id = self._channel_parent_key(message.channel)
        session_key = None
        if parent_channel_id is not None:
            metadata["parent_channel_id"] = parent_channel_id
            metadata["context_chat_id"] = parent_channel_id
            metadata["thread_id"] = channel_id
            session_key = f"{self.name}:{parent_channel_id}:thread:{channel_id}"

        if authorized:
            await self._start_typing(message.channel)

            # Add read receipt reaction immediately, working emoji after delay
            try:
                await message.add_reaction(self.config.read_receipt_emoji)
                self._pending_reactions[channel_id] = message
            except Exception as e:
                self.logger.debug("Failed to add read receipt reaction: {}", e)

            # Delayed working indicator (cosmetic — not tied to subagent lifecycle)
            async def _delayed_working_emoji() -> None:
                await asyncio.sleep(self.config.working_emoji_delay)
                with suppress(Exception):
                    await message.add_reaction(self.config.working_emoji)

            self._working_emoji_tasks[channel_id] = asyncio.create_task(_delayed_working_emoji())

        try:
            await self._handle_message(
                sender_id=sender_id,
                chat_id=channel_id,
                content=full_content,
                media=media_paths,
                metadata=metadata,
                session_key=session_key,
                is_dm=message.guild is None,
            )
        except Exception:
            await self._clear_reactions(channel_id)
            await self._stop_typing(channel_id)
            raise

    async def _on_message(self, message: discord.Message) -> None:
        """Backward-compatible alias for legacy tests/callers."""
        await self._handle_discord_message(message)

    async def _resolve_channel(self, chat_id: str) -> Any | None:
        """Resolve a Discord channel from cache first, then network fetch."""
        client = self._client
        if client is None or not client.is_ready():
            return None
        channel = self._known_channels.get(chat_id)
        if channel is not None:
            return channel
        channel_id = int(chat_id)
        channel = client.get_channel(channel_id)
        if channel is not None:
            return channel
        try:
            return await client.fetch_channel(channel_id)
        except Exception as e:
            self.logger.warning("channel {} unavailable: {}", chat_id, e)
            return None

    async def _finalize_stream(self, chat_id: str, buf: _StreamBuf) -> None:
        """Commit the final streamed content and flush overflow chunks."""
        chunks = DiscordBotClient._build_chunks(buf.text, [], False)
        if not chunks:
            self._stream_bufs.pop(chat_id, None)
            return

        try:
            await buf.message.edit(content=chunks[0])
        except Exception as e:
            self.logger.warning("final stream edit failed: {}", e)
            raise

        target = getattr(buf.message, "channel", None) or await self._resolve_channel(chat_id)
        if target is None:
            self.logger.warning("stream follow-up target {} unavailable", chat_id)
            self._stream_bufs.pop(chat_id, None)
            return

        for extra_chunk in chunks[1:]:
            await target.send(content=extra_chunk)

        self._stream_bufs.pop(chat_id, None)
        await self._stop_typing(chat_id)
        await self._clear_reactions(chat_id)

    def _should_accept_inbound(self, message: discord.Message, content: str) -> bool:
        """Channel-policy filter only (allowed channels, group mention policy).

        Sender authorization is deliberately NOT checked here: the central
        bus-ingress gate enforces it uniformly and issues pairing codes for
        unknown DM senders, so the channel must publish unconditionally.
        """
        allow_channels = self.config.allow_channels
        if allow_channels:
            channel_ids = self._channel_allow_keys(message.channel)
            if channel_ids.isdisjoint(allow_channels):
                return False
        if message.guild is not None and not self._should_respond_in_group(message, content):
            return False
        return True

    async def _download_attachments(
        self,
        attachments: list[discord.Attachment],
    ) -> tuple[list[str], list[str]]:
        """Download supported attachments and return paths + display markers."""
        media_paths: list[str] = []
        markers: list[str] = []
        media_dir = get_media_dir("discord")

        for attachment in attachments:
            filename = attachment.filename or "attachment"
            if attachment.size and attachment.size > MAX_ATTACHMENT_BYTES:
                markers.append(f"[attachment: {filename} - too large]")
                continue
            try:
                media_dir.mkdir(parents=True, exist_ok=True)
                safe_name = safe_filename(filename)
                file_path = media_dir / f"{attachment.id}_{safe_name}"
                await attachment.save(file_path)
                if self._is_audio_attachment(attachment):
                    transcription = await self.transcribe_audio(file_path)
                    if transcription:
                        # Bare transcript, no marker and no audio path: the agent
                        # should read it as what the user said, not as a file it
                        # must open (matches the Telegram voice path).
                        markers.append(transcription)
                        continue
                media_paths.append(str(file_path))
                markers.append(f"[attachment: {file_path.name}]")
            except Exception as e:
                self.logger.warning("Failed to download attachment: {}", e)
                markers.append(f"[attachment: {filename} - download failed]")

        return media_paths, markers

    @staticmethod
    def _compose_inbound_content(content: str, attachment_markers: list[str]) -> str:
        """Combine message text with attachment markers."""
        content_parts = [content] if content else []
        content_parts.extend(attachment_markers)
        return "\n".join(part for part in content_parts if part) or "[empty message]"

    @staticmethod
    def _is_system_message(message: discord.Message) -> bool:
        """Return True for Discord system messages that carry no user prompt."""
        message_type = getattr(message, "type", discord.MessageType.default)
        return message_type not in {discord.MessageType.default, discord.MessageType.reply}

    @staticmethod
    def _build_inbound_metadata(message: discord.Message) -> dict[str, str | None]:
        """Build metadata for inbound Discord messages."""
        reply_to = (
            str(message.reference.message_id)
            if message.reference and message.reference.message_id
            else None
        )
        return {
            "message_id": str(message.id),
            "guild_id": str(message.guild.id) if message.guild else None,
            "reply_to": reply_to,
        }

    def _should_respond_in_group(self, message: discord.Message, content: str) -> bool:
        """Check if the bot should respond in a guild channel based on policy."""
        if self.config.group_policy == "open":
            return True

        if self.config.group_policy == "mention":
            bot_user_id = self._bot_user_id
            if bot_user_id is None and self._client and self._client.user:
                bot_user_id = str(self._client.user.id)
            if bot_user_id is None:
                self.logger.debug(
                    "message in {} ignored (bot identity unavailable)", message.channel.id
                )
                return False

            if any(str(user.id) == bot_user_id for user in message.mentions):
                return True
            if bot_user_id in {str(user_id) for user_id in getattr(message, "raw_mentions", [])}:
                return True
            if f"<@{bot_user_id}>" in content or f"<@!{bot_user_id}>" in content:
                return True
            if self._references_bot_message(message, bot_user_id):
                return True

            self.logger.debug("message in {} ignored (bot not mentioned)", message.channel.id)
            return False

        return True

    @staticmethod
    def _references_bot_message(message: discord.Message, bot_user_id: str) -> bool:
        """Return True when a Discord reply targets a message authored by this bot."""
        reference = getattr(message, "reference", None)
        if reference is None:
            return False
        referenced_message = getattr(reference, "resolved", None) or getattr(
            reference, "cached_message", None
        )
        author = getattr(referenced_message, "author", None)
        return str(getattr(author, "id", "")) == bot_user_id

    async def _start_typing(self, channel: Messageable) -> None:
        """Start periodic typing indicator for a channel."""
        channel_id = self._channel_key(channel)
        await self._stop_typing(channel_id)

        async def typing_loop() -> None:
            while self._running:
                try:
                    async with channel.typing():
                        await asyncio.sleep(TYPING_INTERVAL_S)
                except asyncio.CancelledError:
                    return
                except Exception as e:
                    self.logger.debug("typing indicator failed for {}: {}", channel_id, e)
                    return

        self._typing_tasks[channel_id] = asyncio.create_task(typing_loop())

    async def _stop_typing(self, channel_id: str) -> None:
        """Stop typing indicator for a channel."""
        task = self._typing_tasks.pop(self._channel_key(channel_id), None)
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _clear_reactions(self, chat_id: str) -> None:
        """Remove all pending reactions after bot replies."""
        # Cancel delayed working emoji if it hasn't fired yet
        task = self._working_emoji_tasks.pop(chat_id, None)
        if task and not task.done():
            task.cancel()

        msg_obj = self._pending_reactions.pop(chat_id, None)
        if msg_obj is None:
            return
        bot_user = self._client.user if self._client else None
        for emoji in (self.config.read_receipt_emoji, self.config.working_emoji):
            with suppress(Exception):
                await msg_obj.remove_reaction(emoji, bot_user)

    async def _cancel_all_typing(self) -> None:
        """Stop all typing tasks."""
        channel_ids = list(self._typing_tasks)
        for channel_id in channel_ids:
            await self._stop_typing(channel_id)

    def _start_liveness_probe(self) -> None:
        self._stop_liveness_probe()
        self._liveness_task = asyncio.create_task(self._liveness_loop())

    def _stop_liveness_probe(self) -> None:
        task, self._liveness_task = self._liveness_task, None
        if task is not None and not task.done():
            task.cancel()

    async def _liveness_loop(self) -> None:
        """Detect zombie sockets the gateway heartbeat cannot see.

        A NAT/proxy that drops the connection without RST leaves the websocket
        "open" forever; a periodic authenticated REST call proves the token
        and the network path are actually alive. After consecutive failures
        the client is force-closed, which ends ``start()`` and lets the
        manager supervisor reconnect with backoff.
        """
        failures = 0
        while self._running:
            await asyncio.sleep(LIVENESS_INTERVAL_S)
            client = self._client
            if client is None or not client.is_ready() or client.user is None:
                failures = 0  # reconnecting: discord.py is already on it
                continue
            try:
                await asyncio.wait_for(
                    client.fetch_user(client.user.id), timeout=LIVENESS_TIMEOUT_S
                )
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                failures += 1
                self.logger.warning(
                    "liveness probe failed ({}/{}): {}", failures, LIVENESS_MAX_FAILURES, e
                )
                if failures >= LIVENESS_MAX_FAILURES:
                    self._liveness_failed = True
                    self.logger.error("connection unresponsive; forcing reconnect")
                    with suppress(Exception):
                        await client.close()
                    return

    async def _reset_runtime_state(self, close_client: bool) -> None:
        """Reset client and typing state."""
        self._stop_liveness_probe()
        await self._cancel_all_typing()
        self._stream_bufs.clear()
        pending_splits, self._pending_splits = self._pending_splits, {}
        for key, buf in pending_splits.items():
            # Same current-task guard as _flush_split: a buffer's own timer
            # task could in principle be the one calling this (e.g. a future
            # caller triggering teardown from within _flush_later); cancelling
            # your own running task doesn't raise until the next suspension
            # point, which would land inside the dispatch below.
            if buf.task is not None and buf.task is not asyncio.current_task() and not buf.task.done():
                buf.task.cancel()
            # Flush rather than drop: dedup already marked the buffered
            # message seen, so discarding it here would lose it permanently.
            # Publishing to the bus does not need a live client.
            try:
                await self._dispatch_inbound(buf.message, "\n".join(buf.parts))
            except Exception:
                self.logger.exception("Failed to flush buffered split message for {}", key)
        self._known_channels.clear()
        if close_client and self._client is not None and not self._client.is_closed():
            try:
                await self._client.close()
            except Exception as e:
                self.logger.warning("client close failed: {}", e)
        self._client = None
        self._bot_user_id = None
        self._release_token_lock()
