"""WhatsAppService — browser-based QR pairing for the WhatsApp channel.

WhatsApp links via a QR code scanned from the phone. These routes run the
bridge in `qr --emit-frames` mode and stream the QR code and pairing status to
the settings UI, so the user never touches the terminal. The flow mirrors the
Codex device-code OAuth pattern: ``start`` kicks off pairing, the webui polls
``poll`` until the status turns ``connected``, then enables + starts the
channel through the normal channels-runtime routes.
"""

from __future__ import annotations

from durin.service.principal import Principal, Scope
from durin.service.registry import route
from durin.service.types import Command, Query, Result


class WhatsAppLoginStartCommand(Command):
    #: clear any existing linked session and pair fresh
    force: bool = False


class WhatsAppLoginState(Result):
    #: idle | starting | waiting_scan | connected | timeout | already_paired | error
    status: str
    #: the QR payload to render, present only while status == waiting_scan
    qr: str | None = None
    error: str | None = None


class WhatsAppLoginPollQuery(Query):
    pass


class WhatsAppService:
    """HTTP surface for WhatsApp QR pairing."""

    def __init__(self) -> None:
        # One pairing attempt at a time is enough — the settings UI drives a
        # single wizard. Created lazily so importing the service never touches
        # the bridge binary or auth dir.
        self._session = None

    def _get_session(self):
        if self._session is None:
            from durin.channels.whatsapp import _bridge_token_path, _load_or_create_bridge_token
            from durin.channels.whatsapp_bridge import PairingSession

            auth_dir = _bridge_token_path().parent
            token = _load_or_create_bridge_token(_bridge_token_path())
            self._session = PairingSession(auth_dir=auth_dir, token=token)
        return self._session

    @route(
        "POST",
        "/api/v1/channels/whatsapp/login/start",
        scope=Scope.CONFIG_WRITE.value,
        request_model=WhatsAppLoginStartCommand,
        response_model=WhatsAppLoginState,
        summary="Start WhatsApp QR pairing (runs the bridge, streams the QR)",
    )
    async def login_start(
        self, cmd: WhatsAppLoginStartCommand, principal: Principal
    ) -> WhatsAppLoginState:
        principal.require(Scope.CONFIG_WRITE)
        snap = await self._get_session().start(force=cmd.force)
        return WhatsAppLoginState(**snap)

    @route(
        "GET",
        "/api/v1/channels/whatsapp/login/poll",
        scope=Scope.CONFIG_READ.value,
        request_model=WhatsAppLoginPollQuery,
        response_model=WhatsAppLoginState,
        summary="Poll the current WhatsApp pairing status and QR code",
    )
    async def login_poll(
        self, query: WhatsAppLoginPollQuery, principal: Principal
    ) -> WhatsAppLoginState:
        principal.require(Scope.CONFIG_READ)
        return WhatsAppLoginState(**self._get_session().snapshot())
