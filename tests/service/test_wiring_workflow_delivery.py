"""The registry the dashboard and the API are served from reports a resumed
workflow run to the conversation that launched it."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from unittest.mock import AsyncMock

from starlette.testclient import TestClient

from durin.service.types import NotFoundError
from durin.service.wiring import build_service_registry
from durin.service.workflows import WorkflowsService


@dataclass
class _FakeSessionManager:
    workspace: object


async def _deliver(launch, name, run_id, result, output_files) -> None:
    return None


def test_the_workflows_service_gets_the_callback_it_is_given(tmp_path):
    registry = build_service_registry(
        config=None, session_manager=_FakeSessionManager(tmp_path),
        workflow_deliver_result=_deliver, reconcile_runs=False)
    assert registry.get("workflows")._deliver_result is _deliver


def test_without_a_callback_the_workflows_service_reports_nowhere(tmp_path):
    registry = build_service_registry(
        config=None, session_manager=_FakeSessionManager(tmp_path), reconcile_runs=False)
    assert registry.get("workflows")._deliver_result is None


def test_after_a_websocket_hot_start_the_workflow_routes_keep_the_gateway_registry(
        tmp_path, monkeypatch):
    """The HTTP app serves /api/v1 from the registry the gateway built at boot.
    A hot-started websocket channel builds a registry of its own, which serves
    none of those routes: a resume through the routes still reaches the
    workflows service holding the callback."""
    from durin.api.asgi import build_gateway_http_app
    from durin.bus.queue import MessageBus
    from durin.channels.manager import ChannelManager
    from durin.config.schema import Config
    from durin.session.manager import SessionManager

    data_dir = tmp_path / "durin_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: data_dir)
    workspace = tmp_path / "ws"
    config = Config.model_validate({
        "agents": {"defaults": {"workspace": str(workspace)}},
        "channels": {"websocket": {
            "enabled": True, "allowFrom": ["*"], "host": "127.0.0.1", "port": 8765,
            "path": "/", "websocketRequiresToken": False}},
    })
    monkeypatch.setattr("durin.config.loader.load_config", lambda *_a, **_kw: config)
    bus = MessageBus()
    sessions = SessionManager(workspace=workspace)
    manager = ChannelManager(config, bus, session_manager=sessions)
    boot_channel = manager.get_channel("websocket")
    deliver = AsyncMock()
    registry = build_service_registry(
        config=config, session_manager=sessions, bus=bus, channel_manager=manager,
        workflow_deliver_result=deliver, reconcile_runs=False)
    app = build_gateway_http_app(boot_channel, registry, auth=registry.get("auth"),
                                 static_token="tok")

    async def _hot_restart() -> None:
        await manager.stop_channel("websocket")
        await manager.start_channel("websocket")

    asyncio.run(asyncio.wait_for(_hot_restart(), timeout=30))
    restarted = manager.get_channel("websocket")
    assert restarted is not None and restarted is not boot_channel

    served: list[WorkflowsService] = []

    async def _execute(self, name, task, *, input_files=None, output_format=None,
                       resume_run_id=None, run_id=None, root_session_key=None,
                       work_key=None, paced=False, launch=None):
        served.append(self)
        raise NotFoundError("recorded")

    monkeypatch.setattr(WorkflowsService, "execute", _execute)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post("/api/v1/workflows/wf/run",
                       json={"name": "wf", "task": "", "resume_run_id": "r1"},
                       headers={"Authorization": "Bearer tok"})
    assert resp.status_code == 404
    assert [s._deliver_result for s in served] == [deliver]
    assert restarted._services.get("workflows")._deliver_result is None
