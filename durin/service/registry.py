"""ServiceRegistry — the container that holds service instances and the route
table collected from their ``@route``-decorated methods.

The route table is the single source the OpenAPI generator (SP3) and the
Starlette adapter (SP4) read: one declarative ``RouteSpec`` per HTTP-exposed
service method. The decorator only *annotates* the method, so the method stays a
plain callable and remains directly callable in-process (TUI path, SP7).

SP0 establishes the structure; dependency wiring (config, session_manager, …) is
filled in by SP1 as services are extracted.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from durin.service.types import Command, Query, Result

#: Attribute name under which ``route`` stashes its spec on a method.
ROUTE_ATTR = "__route_spec__"


@dataclass(frozen=True)
class RouteSpec:
    """Declarative metadata pairing a service method with its HTTP surface.

    Read by the OpenAPI generator and the Starlette router; never imports HTTP.
    ``scope`` is a :class:`~durin.service.principal.Scope` *value* (e.g.
    ``"settings:write"``) or ``None`` for an unauthenticated route.
    """

    verb: str  # "GET" | "POST" | "PATCH" | "DELETE"
    path: str  # e.g. "/api/v1/settings"
    scope: str | None
    request_model: type[Command] | type[Query] | None
    response_model: type[Result] | None
    summary: str


def route(
    verb: str,
    path: str,
    *,
    scope: str | None = None,
    request_model: type[Command] | type[Query] | None = None,
    response_model: type[Result] | None = None,
    summary: str = "",
) -> Callable[[Callable], Callable]:
    """Mark a service method as an HTTP route.

    Attaches a :class:`RouteSpec` read at registration time. Returns the method
    unchanged otherwise, so it is still a normal awaitable the TUI can call
    directly with a ``Principal.local()``.
    """

    def deco(fn: Callable) -> Callable:
        setattr(fn, ROUTE_ATTR, RouteSpec(verb, path, scope, request_model, response_model, summary))
        return fn

    return deco


@dataclass
class BoundRoute:
    """A :class:`RouteSpec` bound to the concrete service + method serving it."""

    spec: RouteSpec
    service_name: str
    handler: Callable


class ServiceRegistry:
    """Holds service instances and the route table collected from them.

    Dependencies (``config``, ``session_manager``, ``cron_service``, ``bus``) are
    optional and injected at construction — the same pattern the websocket
    channel uses (``durin/channels/websocket.py``). They are stored for services
    to read; SP0 wires nothing through them yet.
    """

    def __init__(
        self,
        *,
        config: Any = None,
        session_manager: Any = None,
        cron_service: Any = None,
        bus: Any = None,
        channel_manager: Any = None,
        loops_runtime: Any = None,
    ) -> None:
        self.config = config
        self.session_manager = session_manager
        self.cron_service = cron_service
        self.bus = bus
        self.channel_manager = channel_manager
        self.loops_runtime = loops_runtime
        self._services: dict[str, Any] = {}
        self._routes: list[BoundRoute] = []

    def register(self, name: str, service: Any) -> None:
        """Register a service instance and collect its ``@route`` methods.

        Raises ``ValueError`` on a duplicate name or a duplicate (verb, path)
        — both are wiring bugs that must fail loudly at startup, not silently
        shadow an earlier route.
        """
        if name in self._services:
            raise ValueError(f"service already registered: {name}")
        self._services[name] = service
        for attr_name in dir(service):
            if attr_name.startswith("__"):
                continue
            attr = getattr(service, attr_name)
            spec: RouteSpec | None = getattr(attr, ROUTE_ATTR, None)
            if spec is None:
                continue
            if self.route_for(spec.verb, spec.path) is not None:
                raise ValueError(f"duplicate route: {spec.verb} {spec.path}")
            self._routes.append(BoundRoute(spec=spec, service_name=name, handler=attr))

    def get(self, name: str) -> Any:
        """Return a registered service instance (``KeyError`` if absent)."""
        return self._services[name]

    @property
    def routes(self) -> list[BoundRoute]:
        """All collected routes, in registration order."""
        return list(self._routes)

    def route_for(self, verb: str, path: str) -> BoundRoute | None:
        """Find a bound route by verb + path, or ``None``."""
        for r in self._routes:
            if r.spec.verb == verb and r.spec.path == path:
                return r
        return None
