"""WhatsApp channel implementation using a Go bridge (whatsmeow)."""

import asyncio
import json
import mimetypes
import os
import random
import secrets
from collections import OrderedDict
from contextlib import suppress
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field

from durin.bus.events import OutboundMessage
from durin.bus.queue import MessageBus
from durin.channels.base import BaseChannel
from durin.channels.whatsapp_format import chunk_message, markdown_to_whatsapp
from durin.config.schema import Base

ACK_TIMEOUT = 30.0


def _next_backoff(delay: float, *, factor: float = 1.6, cap: float = 30.0) -> float:
    return min(delay * factor, cap)


def _sanitize_media_paths(paths: list[str], media_dir: Path, logger) -> list[str]:
    """Drop any inbound media path that doesn't resolve under ``media_dir``.

    Defense in depth against a compromised or buggy bridge reporting a
    path-traversal media path (the bridge is expected to sandbox writes to
    its own media dir, but the channel must not trust that blindly)."""
    resolved_dir = media_dir.resolve()
    safe: list[str] = []
    for p in paths:
        resolved = Path(p).resolve()
        try:
            resolved.relative_to(resolved_dir)
        except ValueError:
            logger.warning("Dropping inbound media path outside media dir: {}", p)
            continue
        safe.append(p)
    return safe


class WhatsAppConfig(Base):
    """WhatsApp channel configuration."""

    enabled: bool = False
    bridge_url: str = "ws://localhost:3001"
    bridge_token: str = ""
    # Pairing is done through the guided UI / `durin channels login whatsapp`;
    # only these two are meaningful to edit by hand, so only they carry the UI
    # metadata that surfaces them in the manual-mode settings form.
    allow_from: list[str] = Field(
        default_factory=list, json_schema_extra={"group": "access"}
    )
    group_policy: Literal["open", "mention"] = Field(
        "open", json_schema_extra={"group": "behavior"}
    )  # "open" responds to all group messages, "mention" only when @mentioned


def _bridge_token_path() -> Path:
    from durin.config.paths import get_runtime_subdir

    return get_runtime_subdir("whatsapp-auth") / "bridge-token"


def _load_or_create_bridge_token(path: Path) -> str:
    """Load a persisted bridge token or create one on first use."""
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token

    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    path.write_text(token, encoding="utf-8")
    with suppress(OSError):
        path.chmod(0o600)
    return token


class WhatsAppChannel(BaseChannel):
    """
    WhatsApp channel that supervises a Go bridge process and connects to it.

    The bridge uses whatsmeow to handle the WhatsApp Web protocol.
    Communication between Python and the bridge is via WebSocket.
    """

    name = "whatsapp"
    display_name = "WhatsApp"

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return WhatsAppConfig().model_dump(by_alias=False)

    @classmethod
    def config_model(cls) -> type | None:
        # Supplies the manual-mode field form behind the guided pairing panel.
        return WhatsAppConfig

    def __init__(self, config: Any, bus: MessageBus):
        if isinstance(config, dict):
            config = WhatsAppConfig.model_validate(config)
        super().__init__(config, bus)
        self._ws = None
        self._connected = False
        self._processed_message_ids: OrderedDict[str, None] = OrderedDict()
        self._lid_to_phone: dict[str, str] = {}
        # Inbound message_id -> the participant JID to quote when replying:
        # the group's real participant JID for group messages, the sender
        # JID for DMs. ContextInfo.Participant must never be the group JID.
        self._reply_participants: OrderedDict[str, str] = OrderedDict()
        self._bridge_token: str | None = None
        self._pending_acks: dict[str, asyncio.Future] = {}
        self._supervisor = None

    def _effective_bridge_token(self) -> str:
        """Resolve the bridge token, generating a local secret when needed."""
        if self._bridge_token is not None:
            return self._bridge_token
        configured = self.config.bridge_token.strip()
        if configured:
            self._bridge_token = configured
        else:
            self._bridge_token = _load_or_create_bridge_token(_bridge_token_path())
        return self._bridge_token

    async def login(self, force: bool = False) -> bool:
        """Pair with WhatsApp: run the bridge in QR mode in the foreground."""
        from durin.channels.whatsapp_bridge import BridgeSetupError, ensure_bridge_binary

        try:
            binary = await ensure_bridge_binary()
        except BridgeSetupError:
            self.logger.exception("bridge setup failed")
            return False

        auth_dir = _bridge_token_path().parent
        if force:
            db = auth_dir / "whatsmeow.db"
            if db.exists():
                db.unlink()

        proc = await asyncio.create_subprocess_exec(
            str(binary), "qr", "--auth-dir", str(auth_dir),
            env={**os.environ, "BRIDGE_TOKEN": self._effective_bridge_token()},
        )
        return (await proc.wait()) == 0

    async def start(self) -> None:
        """Start the channel: supervise the bridge process and connect."""
        from urllib.parse import urlparse

        import websockets

        from durin.channels.whatsapp_bridge import BridgeSupervisor, ensure_bridge_binary
        from durin.config.paths import get_media_dir

        self._running = True
        try:
            binary = await ensure_bridge_binary()
        except Exception:
            self.logger.exception("WhatsApp bridge setup failed; channel not started")
            self._running = False
            return

        port = urlparse(self.config.bridge_url).port or 3001
        self._supervisor = BridgeSupervisor(
            binary, port=port, token=self._effective_bridge_token(),
            auth_dir=_bridge_token_path().parent,
            # Under the allowlisted media root so agent tools can read
            # inbound attachments (matches every other channel).
            media_dir=get_media_dir("whatsapp"),
            logger=self.logger,
        )
        await self._supervisor.start()

        delay = 2.0
        while self._running:
            if self._supervisor.needs_login:
                self.logger.error("WhatsApp needs pairing; channel idle until `durin channels login whatsapp`")
                # Fully stop (running flag, supervisor) so the manager sees the
                # channel as not running and can rebuild it after re-pairing.
                await self.stop()
                break
            try:
                async with websockets.connect(self.config.bridge_url) as ws:
                    self._ws = ws
                    await ws.send(json.dumps({"type": "auth", "token": self._effective_bridge_token()}))
                    self._connected = True
                    delay = 2.0  # healthy connection: reset backoff
                    self.logger.info("Connected to WhatsApp bridge")
                    async for message in ws:
                        try:
                            await self._handle_bridge_message(message)
                        except Exception:
                            self.logger.exception("Error handling bridge message")
                # Clean close: the bridge or peer ended the connection
                # without raising. Back off exactly like a connection error
                # so two gateways racing for the same bridge don't
                # tight-loop reconnecting against each other.
                self._connected = False
                self._ws = None
                if self._running:
                    delay = await self._backoff_sleep(delay, "Bridge closed connection; reconnecting in {:.1f} seconds...")
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._connected = False
                self._ws = None
                self.logger.warning("WhatsApp bridge connection error: {}", e)
                if self._running:
                    delay = await self._backoff_sleep(delay, "Reconnecting in {:.1f} seconds...")

    async def _backoff_sleep(self, delay: float, message: str) -> float:
        """Jittered backoff sleep before a reconnect attempt; returns the
        next delay in the exponential sequence."""
        jittered = delay + random.uniform(0, delay / 4)
        self.logger.info(message, jittered)
        await asyncio.sleep(jittered)
        return _next_backoff(delay)

    async def stop(self) -> None:
        """Stop the WhatsApp channel."""
        self._running = False
        self._connected = False
        if self._ws:
            await self._ws.close()
            self._ws = None
        sup = getattr(self, "_supervisor", None)
        if sup is not None:
            await sup.stop()
            self._supervisor = None

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through WhatsApp. Raises on failure so the channel
        manager's retry policy applies."""
        if not self._ws or not self._connected:
            raise RuntimeError("WhatsApp bridge not connected")

        chat_id = msg.chat_id

        if msg.content:
            text = markdown_to_whatsapp(msg.content)
            for i, chunk in enumerate(chunk_message(text)):
                payload: dict = {"type": "send", "to": chat_id, "text": chunk}
                if i == 0 and msg.reply_to:
                    payload["reply_to"] = msg.reply_to
                    participant = self._reply_participants.get(msg.reply_to)
                    if participant:
                        payload["reply_to_participant"] = participant
                await self._send_frame_with_ack(payload)

        for media_path in msg.media or []:
            mime, _ = mimetypes.guess_type(media_path)
            await self._send_frame_with_ack({
                "type": "send_media",
                "to": chat_id,
                "filePath": media_path,
                "mimetype": mime or "application/octet-stream",
                "fileName": media_path.rsplit("/", 1)[-1],
            })

        await self._send_typing(chat_id, "paused")

    async def _send_frame_with_ack(self, payload: dict) -> None:
        """Send a frame with a uuid4 id and await the bridge's ack for it.

        Raises RuntimeError on timeout or an explicit not-ok ack. The pending
        future is always removed from ``_pending_acks``, even on timeout, so
        a slow/never-acked frame cannot leak an entry forever.
        """
        frame_id = uuid4().hex
        payload = {**payload, "id": frame_id}
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending_acks[frame_id] = fut
        try:
            await self._ws.send(json.dumps(payload, ensure_ascii=False))
            ack = await asyncio.wait_for(fut, ACK_TIMEOUT)
        except asyncio.TimeoutError as exc:
            raise RuntimeError(f"bridge did not ack {payload['type']} within {ACK_TIMEOUT}s") from exc
        finally:
            self._pending_acks.pop(frame_id, None)
        if not ack.get("ok"):
            raise RuntimeError(f"bridge rejected {payload['type']}: {ack.get('error')}")

    async def _send_typing(self, chat_id: str, state: str) -> None:
        """Fire-and-forget presence hint; never fails a send."""
        if not self._ws:
            return
        with suppress(Exception):
            await self._ws.send(json.dumps({"type": "typing", "to": chat_id, "state": state}))

    async def _handle_bridge_message(self, raw: str) -> None:
        """Handle a message from the bridge."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            self.logger.warning("Invalid JSON from bridge: {}", raw[:100])
            return

        msg_type = data.get("type")

        if msg_type == "ack":
            fut = self._pending_acks.get(data.get("id", ""))
            if fut and not fut.done():
                fut.set_result(data)
            return

        if msg_type == "message":
            # Incoming message from WhatsApp
            # Deprecated by whatsapp: old phone number style typically: <phone>@s.whatspp.net
            pn = data.get("pn", "")
            # New LID sytle typically:
            sender = data.get("sender", "")
            content = data.get("content", "")
            message_id = data.get("id", "")

            # Extract just the phone number or lid as chat_id
            is_group = data.get("isGroup", False)
            was_mentioned = data.get("wasMentioned", False)

            if is_group and getattr(self.config, "group_policy", "open") == "mention":
                if not was_mentioned:
                    return

            # Classify by JID server: s.whatsapp.net = phone, lid or
            # lid.whatsapp.net = LID (whatsmeow emits the bare "@lid" form;
            # older bridges may still send "@lid.whatsapp.net").
            raw_a = pn or ""
            raw_b = sender or ""
            id_a = raw_a.rsplit("@", 1)[0] if "@" in raw_a else raw_a
            id_b = raw_b.rsplit("@", 1)[0] if "@" in raw_b else raw_b

            phone_id = ""
            lid_id = ""
            for raw, extracted in [(raw_a, id_a), (raw_b, id_b)]:
                server = raw.rsplit("@", 1)[1] if "@" in raw else ""
                if server == "s.whatsapp.net":
                    phone_id = extracted
                elif server in ("lid", "lid.whatsapp.net"):
                    lid_id = extracted
                elif extracted and not phone_id:
                    phone_id = extracted  # best guess for bare values

            sender_id = phone_id or self._lid_to_phone.get(lid_id, "") or lid_id or id_a or id_b

            if message_id:
                if message_id in self._processed_message_ids:
                    return
                self._processed_message_ids[message_id] = None
                while len(self._processed_message_ids) > 1000:
                    self._processed_message_ids.popitem(last=False)

                # Cache the participant JID to quote when replying to this
                # message: the real participant for groups (carried in pn),
                # the sender itself for DMs.
                participant_jid = raw_a if is_group else raw_b
                if participant_jid:
                    self._reply_participants[message_id] = participant_jid
                    while len(self._reply_participants) > 1000:
                        self._reply_participants.popitem(last=False)

            if phone_id and lid_id:
                self._lid_to_phone[lid_id] = phone_id

            self.logger.info("Sender phone={} lid={} → sender_id={}", phone_id or "(empty)", lid_id or "(empty)", sender_id)

            await self._send_typing(sender, "composing")

            # Extract media paths (images/documents/videos downloaded by the bridge)
            media_paths = data.get("media") or []
            if media_paths:
                from durin.config.paths import get_media_dir

                media_paths = _sanitize_media_paths(media_paths, get_media_dir("whatsapp"), self.logger)

            # Handle voice transcription if it's a voice message. The bridge's
            # explicit "voice" flag is the current signal; the legacy
            # "[Voice Message]" sentinel content is kept for older bridges.
            if data.get("voice") or content == "[Voice Message]":
                if media_paths:
                    self.logger.info("Transcribing voice message from {}...", sender_id)
                    transcription = await self.transcribe_audio(media_paths[0])
                    if transcription:
                        content = transcription
                        media_paths = []
                        self.logger.info("Transcribed voice from {}: {}...", sender_id, transcription[:50])
                    else:
                        content = "[Voice Message: Transcription failed]"
                else:
                    content = "[Voice Message: Audio not available]"

            # Build content tags matching Telegram's pattern: [image: /path] or [file: /path]
            if media_paths:
                for p in media_paths:
                    mime, _ = mimetypes.guess_type(p)
                    media_type = "image" if mime and mime.startswith("image/") else "file"
                    media_tag = f"[{media_type}: {p}]"
                    content = f"{content}\n{media_tag}" if content else media_tag

            await self._handle_message(
                sender_id=sender_id,
                chat_id=sender,  # Use full LID for replies
                content=content,
                media=media_paths,
                metadata={
                    "message_id": message_id,
                    "timestamp": data.get("timestamp"),
                    "is_group": data.get("isGroup", False),
                    "quoted": data.get("quoted"),
                },
                is_dm=not is_group,
            )

        elif msg_type == "status":
            # Connection status update
            status = data.get("status")
            self.logger.info("Status: {}", status)

            if status == "connected":
                self._connected = True
            elif status == "disconnected":
                self._connected = False

        elif msg_type == "qr":
            # QR code for authentication
            self.logger.info("Scan QR code in the bridge terminal to connect WhatsApp")

        elif msg_type == "error":
            self.logger.error("Bridge error: {}", data.get("error"))
