"""Auto-discovery for built-in channel modules and external plugins."""

from __future__ import annotations

import importlib
import pkgutil
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from durin.channels.base import BaseChannel

_INTERNAL = frozenset({"base", "manager", "registry"})


def discover_channel_names() -> list[str]:
    """Return all built-in channel module names by scanning the package (zero imports)."""
    import durin.channels as pkg

    return [
        name
        for _, name, ispkg in pkgutil.iter_modules(pkg.__path__)
        if name not in _INTERNAL and not ispkg
    ]


def load_channel_class(module_name: str) -> type[BaseChannel]:
    """Import *module_name* and return the first BaseChannel subclass found."""
    from durin.channels.base import BaseChannel as _Base

    mod = importlib.import_module(f"durin.channels.{module_name}")
    for attr in dir(mod):
        obj = getattr(mod, attr)
        if isinstance(obj, type) and issubclass(obj, _Base) and obj is not _Base:
            return obj
    raise ImportError(f"No BaseChannel subclass in durin.channels.{module_name}")


def discover_plugins() -> dict[str, type[BaseChannel]]:
    """Discover external channel plugins registered via entry_points."""
    from importlib.metadata import entry_points

    plugins: dict[str, type[BaseChannel]] = {}
    for ep in entry_points(group="durin.channels"):
        try:
            cls = ep.load()
            plugins[ep.name] = cls
        except Exception as e:
            logger.warning("Failed to load channel plugin '{}': {}", ep.name, e)
    return plugins


def _plugin_entry_points() -> dict[str, Any]:
    """Installed channel plugins' entry points by name, none of them loaded."""
    from importlib.metadata import entry_points

    return {ep.name: ep for ep in entry_points(group="durin.channels")}


def available_channel_names() -> list[str]:
    """Names of every channel durin can start — built-in modules, then
    plugins — without importing any of them.

    A plugin registered under a built-in's name is left out: built-ins win,
    as in ``discover_all``.
    """
    builtin = discover_channel_names()
    plugins = _plugin_entry_points()
    shadowed = set(plugins) & set(builtin)
    if shadowed:
        logger.warning("Plugin(s) shadowed by built-in channels (ignored): {}", shadowed)
    return builtin + [name for name in plugins if name not in shadowed]


def load_channel(name: str) -> type[BaseChannel] | None:
    """Import only the channel called *name* and return its class.

    The built-in module of that name wins over a plugin registered under it.
    Returns None when neither exists; an import or plugin-load failure is
    raised, so the caller can say why the channel is unavailable.
    """
    if name in discover_channel_names():
        return load_channel_class(name)
    ep = _plugin_entry_points().get(name)
    return ep.load() if ep is not None else None


def discover_all() -> dict[str, type[BaseChannel]]:
    """Return all channels: built-in (pkgutil) merged with external (entry_points).

    Built-in channels take priority — an external plugin cannot shadow a built-in name.
    """
    builtin: dict[str, type[BaseChannel]] = {}
    for modname in discover_channel_names():
        try:
            builtin[modname] = load_channel_class(modname)
        except ImportError as e:
            logger.debug("Skipping built-in channel '{}': {}", modname, e)

    external = discover_plugins()
    shadowed = set(external) & set(builtin)
    if shadowed:
        logger.warning("Plugin(s) shadowed by built-in channels (ignored): {}", shadowed)

    return {**external, **builtin}
