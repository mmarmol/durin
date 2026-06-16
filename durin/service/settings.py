"""SettingsService — read and mutate agent model/provider and web-search settings.

Wraps ``load_config`` / ``save_config`` + the provider registry + the secret
store.  ``_payload()`` builds the full settings dict that all four routes
return; it is the authoritative settings assembler for this domain.

**Codex dependency**: ``_payload()`` calls ``codex_token_present()`` directly
from ``durin.providers.codex_device_auth`` (a pure predicate, no OAuth flow).
The OAuth *flow* endpoints (start/poll/disconnect) are a separate domain (SP1
step 10); they are NOT extracted here.

``dict[str, Any]`` escape hatches
----------------------------------
``SettingsResult.providers`` and ``SettingsResult.web_search`` are typed as
``dict[str, Any]`` / ``list[dict[str, Any]]`` because the shapes are built
dynamically from the provider registry and the web-search option tables.
SP3 can tighten these into proper sub-models once the wire shape is frozen.

Extracted from ``durin/channels/websocket.py``
(``_settings_payload`` / ``_handle_settings`` / ``_handle_settings_update`` /
``_handle_settings_provider_update`` / ``_handle_settings_web_search_update``)
in SP1; the channel keeps wire-identical shims.
"""

from __future__ import annotations

from typing import Any

from durin.service.principal import Principal, Scope
from durin.service.registry import route
from durin.service.types import (
    Command,
    Query,
    Result,
    ValidationFailedError,
)

# ---------------------------------------------------------------------------
# Web-search provider tables (copied from websocket.py module scope so the
# service has no import dependency on the channel).
# ---------------------------------------------------------------------------

_WEB_SEARCH_PROVIDER_OPTIONS: tuple[dict[str, str], ...] = (
    {"name": "duckduckgo", "label": "DuckDuckGo", "credential": "none"},
    {"name": "brave", "label": "Brave Search", "credential": "api_key"},
    {"name": "tavily", "label": "Tavily", "credential": "api_key"},
    {"name": "searxng", "label": "SearXNG", "credential": "base_url"},
    {"name": "jina", "label": "Jina", "credential": "api_key"},
    {"name": "kagi", "label": "Kagi", "credential": "api_key"},
    {"name": "olostep", "label": "Olostep", "credential": "api_key"},
)
_WEB_SEARCH_PROVIDER_BY_NAME: dict[str, dict[str, str]] = {
    p["name"]: p for p in _WEB_SEARCH_PROVIDER_OPTIONS
}


# ---------------------------------------------------------------------------
# DTOs — settings read
# ---------------------------------------------------------------------------


class SettingsQuery(Query):
    """No inputs — returns the full settings payload."""


class SettingsResult(Result):
    """Full settings payload.

    ``agent``, ``providers``, ``web_search``, and ``runtime`` mirror the keys
    the existing handler returns.  The nested structures are ``dict[str, Any]``
    escape hatches — SP3 can tighten them.
    """

    agent: dict[str, Any]
    providers: list[dict[str, Any]]
    web_search: dict[str, Any]
    runtime: dict[str, Any]
    requires_restart: bool


# ---------------------------------------------------------------------------
# DTOs — settings/update
# ---------------------------------------------------------------------------


class SettingsUpdateCommand(Command):
    model: str | None = None
    provider: str | None = None


# ---------------------------------------------------------------------------
# DTOs — settings/provider/update
# ---------------------------------------------------------------------------


class SettingsProviderUpdateCommand(Command):
    provider: str
    api_key: str | None = None
    api_base: str | None = None


# ---------------------------------------------------------------------------
# DTOs — settings/web-search/update
# ---------------------------------------------------------------------------


class SettingsWebSearchUpdateCommand(Command):
    provider: str
    api_key: str | None = None
    base_url: str | None = None


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class SettingsService:
    """Read and mutate agent model/provider and web-search settings."""

    def _payload(self, *, requires_restart: bool = False) -> SettingsResult:
        """Build the full settings dict.

        This is the moved body of ``WebSocketChannel._settings_payload``.
        """
        from durin.config.loader import get_config_path, load_config
        from durin.providers.registry import PROVIDERS, find_by_name
        from durin.security.secrets import mask_secret_hint

        config = load_config()
        defaults = config.agents.defaults
        provider_name = config.get_provider_name(defaults.model) or defaults.provider
        provider = config.get_provider(defaults.model)
        selected_provider = provider_name
        if defaults.provider != "auto":
            spec = find_by_name(defaults.provider)
            selected_provider = spec.name if spec else provider_name

        from durin.providers.codex_device_auth import codex_token_present

        providers: list[dict[str, Any]] = []
        for spec in PROVIDERS:
            if spec.name == "openai_codex":
                providers.append(
                    {
                        "name": spec.name,
                        "label": spec.label,
                        "configured": codex_token_present(),
                        "oauth": True,
                    }
                )
                continue
            provider_config = getattr(config.providers, spec.name, None)
            if provider_config is None or spec.is_oauth or spec.is_local:
                continue
            providers.append(
                {
                    "name": spec.name,
                    "label": spec.label,
                    "configured": bool(provider_config.api_key),
                    "api_key_hint": mask_secret_hint(provider_config.api_key),
                    "api_base": provider_config.api_base,
                    "default_api_base": spec.default_api_base or None,
                }
            )

        search_config = config.tools.web.search
        search_provider = (
            search_config.provider
            if search_config.provider in _WEB_SEARCH_PROVIDER_BY_NAME
            else "duckduckgo"
        )

        return SettingsResult(
            agent={
                "model": defaults.model,
                "provider": selected_provider,
                "resolved_provider": provider_name,
                "has_api_key": bool(provider and provider.api_key),
            },
            providers=providers,
            web_search={
                "provider": search_provider,
                "api_key_hint": mask_secret_hint(search_config.api_key),
                "base_url": search_config.base_url or None,
                "providers": list(_WEB_SEARCH_PROVIDER_OPTIONS),
            },
            runtime={
                "config_path": str(get_config_path().expanduser()),
            },
            requires_restart=requires_restart,
        )

    @route(
        "GET",
        "/api/settings",
        scope=Scope.SETTINGS_READ.value,
        request_model=SettingsQuery,
        response_model=SettingsResult,
        summary="Return the full settings payload (secrets masked)",
    )
    async def get(self, query: SettingsQuery, principal: Principal) -> SettingsResult:
        principal.require(Scope.SETTINGS_READ)
        return self._payload()

    @route(
        "GET",
        "/api/settings/update",
        scope=Scope.SETTINGS_WRITE.value,
        request_model=SettingsUpdateCommand,
        response_model=SettingsResult,
        summary="Update agent model and/or provider",
    )
    async def update(
        self, cmd: SettingsUpdateCommand, principal: Principal
    ) -> SettingsResult:
        principal.require(Scope.SETTINGS_WRITE)
        from durin.config.loader import load_config, save_config
        from durin.providers.registry import find_by_name

        config = load_config()
        defaults = config.agents.defaults
        changed = False

        if cmd.model is not None:
            model = cmd.model.strip()
            if not model:
                raise ValidationFailedError("model is required")
            if defaults.model != model:
                defaults.model = model
                changed = True

        if cmd.provider is not None:
            provider = cmd.provider.strip()
            if not provider:
                raise ValidationFailedError("provider is required")
            spec = find_by_name(provider)
            if spec is None:
                raise ValidationFailedError("unknown provider")
            provider_config = getattr(config.providers, provider, None)
            if spec.name == "openai_codex":
                from durin.providers.codex_device_auth import codex_token_present

                configured = codex_token_present()
            elif getattr(spec, "is_oauth", False):
                from durin.utils.oauth import any_token_present

                configured = any_token_present(spec.name)
            else:
                configured = bool(provider_config and provider_config.api_key)
            if not configured:
                raise ValidationFailedError("provider is not configured")
            if defaults.provider != provider:
                defaults.provider = provider
                changed = True

        if changed:
            save_config(config)
        return self._payload(requires_restart=False)

    @route(
        "GET",
        "/api/settings/provider/update",
        scope=Scope.SETTINGS_WRITE.value,
        request_model=SettingsProviderUpdateCommand,
        response_model=SettingsResult,
        summary="Update provider API key and/or base URL (key stored as secret ref)",
    )
    async def provider_update(
        self, cmd: SettingsProviderUpdateCommand, principal: Principal
    ) -> SettingsResult:
        """Update a provider's credentials.

        The ``api_key`` is stored in the secret store and only a
        ``${secret:}`` reference is persisted in config — the dashboard must
        never write plaintext (see ``docs/11_secrets_design.md``).
        """
        principal.require(Scope.SETTINGS_WRITE)
        from durin.config.loader import load_config, save_config
        from durin.providers.registry import find_by_name

        provider_name = cmd.provider.strip()
        if not provider_name:
            raise ValidationFailedError("provider is required")
        spec = find_by_name(provider_name)
        if spec is None or spec.is_oauth or spec.is_local:
            raise ValidationFailedError("unknown provider")

        config = load_config()
        provider_config = getattr(config.providers, spec.name, None)
        if provider_config is None:
            raise ValidationFailedError("unknown provider")

        changed = False

        if cmd.api_key is not None:
            api_key: str | None = cmd.api_key.strip() or None
            from durin.security.secrets import is_secret_ref, store_secret

            if api_key and not is_secret_ref(api_key):
                api_key = store_secret(
                    f"{spec.name}_API_KEY",
                    api_key,
                    service=f"provider:{spec.name}",
                    scope=[f"provider:{spec.name}"],
                    description=f"{spec.name} API key",
                    origin="webui",
                )
            if provider_config.api_key != api_key:
                provider_config.api_key = api_key
                changed = True

        if cmd.api_base is not None:
            api_base: str | None = cmd.api_base.strip() or None
            if provider_config.api_base != api_base:
                provider_config.api_base = api_base
                changed = True

        if changed:
            save_config(config)
        return self._payload(requires_restart=False)

    @route(
        "GET",
        "/api/settings/web-search/update",
        scope=Scope.SETTINGS_WRITE.value,
        request_model=SettingsWebSearchUpdateCommand,
        response_model=SettingsResult,
        summary="Update web-search provider and credentials",
    )
    async def web_search_update(
        self, cmd: SettingsWebSearchUpdateCommand, principal: Principal
    ) -> SettingsResult:
        principal.require(Scope.SETTINGS_WRITE)
        from durin.config.loader import load_config, save_config

        provider_name = cmd.provider.strip().lower()
        provider_option = _WEB_SEARCH_PROVIDER_BY_NAME.get(provider_name)
        if provider_option is None:
            raise ValidationFailedError("unknown web search provider")

        config = load_config()
        search_config = config.tools.web.search
        previous_provider = search_config.provider
        changed = False

        def _set(attr: str, value: str | None) -> None:
            nonlocal changed
            if getattr(search_config, attr) != value:
                setattr(search_config, attr, value)
                changed = True

        if search_config.provider != provider_name:
            search_config.provider = provider_name
            changed = True

        credential = provider_option["credential"]
        if credential == "none":
            _set("api_key", "")
            _set("base_url", "")
        elif credential == "base_url":
            base_url = cmd.base_url.strip() if cmd.base_url is not None else None
            if not base_url and previous_provider == provider_name and search_config.base_url:
                base_url = search_config.base_url
            if not base_url:
                raise ValidationFailedError("base_url is required")
            _set("base_url", base_url)
            _set("api_key", "")
        else:
            api_key = cmd.api_key.strip() if cmd.api_key is not None else None
            if not api_key and previous_provider == provider_name and search_config.api_key:
                api_key = search_config.api_key
            if not api_key:
                raise ValidationFailedError("api_key is required")
            _set("api_key", api_key)
            _set("base_url", "")

        if changed:
            save_config(config)
        return self._payload(requires_restart=False)
