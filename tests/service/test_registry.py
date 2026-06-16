"""SP0: the @route decorator and the ServiceRegistry route collector."""

import asyncio

import pytest

from durin.service.registry import ROUTE_ATTR, BoundRoute, RouteSpec, ServiceRegistry, route


def test_route_decorator_attaches_spec():
    @route("POST", "/api/v1/x", scope="x:write", summary="create x")
    async def handler(self, cmd, principal):  # noqa: ANN001
        return "ok"

    spec = getattr(handler, ROUTE_ATTR)
    assert isinstance(spec, RouteSpec)
    assert spec.verb == "POST"
    assert spec.path == "/api/v1/x"
    assert spec.scope == "x:write"
    assert spec.summary == "create x"


def test_decorated_method_is_still_directly_callable():
    class Svc:
        @route("GET", "/api/v1/ping")
        async def ping(self, query, principal):  # noqa: ANN001
            return "pong"

    # The decorator only annotates; the method remains a normal coroutine the
    # in-process TUI can await directly.
    assert asyncio.run(Svc().ping(None, None)) == "pong"


def test_registry_collects_routes_from_decorated_methods():
    class Svc:
        @route("GET", "/api/v1/a", scope="a:read")
        async def list_a(self, query, principal):  # noqa: ANN001
            ...

        @route("POST", "/api/v1/a", scope="a:write")
        async def create_a(self, cmd, principal):  # noqa: ANN001
            ...

        async def helper(self):  # not a route — must be ignored
            ...

    reg = ServiceRegistry()
    reg.register("svc", Svc())
    assert len(reg.routes) == 2
    assert {(r.spec.verb, r.spec.path) for r in reg.routes} == {
        ("GET", "/api/v1/a"),
        ("POST", "/api/v1/a"),
    }
    assert all(isinstance(r, BoundRoute) and r.service_name == "svc" for r in reg.routes)


def test_register_duplicate_name_raises():
    reg = ServiceRegistry()
    reg.register("svc", object())
    with pytest.raises(ValueError, match="already registered"):
        reg.register("svc", object())


def test_register_duplicate_route_raises():
    class S1:
        @route("GET", "/api/v1/dup")
        async def a(self, query, principal):  # noqa: ANN001
            ...

    class S2:
        @route("GET", "/api/v1/dup")
        async def b(self, query, principal):  # noqa: ANN001
            ...

    reg = ServiceRegistry()
    reg.register("s1", S1())
    with pytest.raises(ValueError, match="duplicate route"):
        reg.register("s2", S2())


def test_get_returns_service_and_raises_on_missing():
    class Svc:
        pass

    svc = Svc()
    reg = ServiceRegistry()
    reg.register("svc", svc)
    assert reg.get("svc") is svc
    with pytest.raises(KeyError):
        reg.get("absent")


def test_route_for_lookup():
    class Svc:
        @route("GET", "/api/v1/a")
        async def a(self, query, principal):  # noqa: ANN001
            ...

    svc = Svc()
    reg = ServiceRegistry()
    reg.register("svc", svc)
    found = reg.route_for("GET", "/api/v1/a")
    assert found is not None
    assert found.handler == svc.a  # bound method to the registered instance
    assert reg.route_for("POST", "/api/v1/a") is None


def test_registry_holds_injected_deps():
    sentinel = object()
    reg = ServiceRegistry(
        config=sentinel,
        session_manager=sentinel,
        cron_service=sentinel,
        bus=sentinel,
    )
    assert reg.config is sentinel
    assert reg.session_manager is sentinel
    assert reg.cron_service is sentinel
    assert reg.bus is sentinel
