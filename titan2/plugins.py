"""The StructureHandler map (spec §14, DETAIL §14.1): phase 1's SingleLegHandler, plus the optional titan2.multileg.

load_handlers() returns {LONG_CE: SingleLegHandler, LONG_PE: SingleLegHandler}. When the phase-2 package exists
(importlib.util.find_spec("titan2.multileg") finds it), its register(handlers, deps) is called with a copy of that
map, and may only add handlers for new structure types. Fails closed: a plugin that cannot be imported, raises, or
returns anything but valid additions raises PluginError, and the runtime runs on phase1_handlers() alone (phase-2
intents are then refused by the gateway's NO_HANDLER check) with a CRITICAL alert.

deps is a read-only mapping of what a plugin may use; the runtime passes settings, calendar, clock and quotes.
"""

from __future__ import annotations

import importlib
import importlib.util
from types import MappingProxyType
from typing import Any, Mapping

from titan2.core.single_leg import SingleLegHandler, StructureHandler
from titan2.model import StructureType

__all__ = ["PLUGIN_MODULE", "PluginError", "load_handlers", "phase1_handlers"]

PLUGIN_MODULE = "titan2.multileg"


class PluginError(Exception):
    """The phase-2 plugin is present but unusable; the runtime keeps the phase-1 handlers only."""


def phase1_handlers() -> dict[StructureType, StructureHandler]:
    single = SingleLegHandler()
    return {StructureType.LONG_CE: single, StructureType.LONG_PE: single}


def _plugin_present(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError) as exc:      # e.g. a parent package that cannot be imported
        raise PluginError(f"cannot look up {name}: {exc!r}") from exc


def load_handlers(deps: Mapping[str, Any] | None = None, *,
                  plugin: str = PLUGIN_MODULE) -> dict[StructureType, StructureHandler]:
    """The handler map the engine and the gateway use (EngineContext.handlers). Raises PluginError (see above)."""
    handlers = phase1_handlers()
    if not _plugin_present(plugin):
        return handlers
    try:
        module = importlib.import_module(plugin)
        register = getattr(module, "register")
        proposed = dict(handlers)
        register(proposed, MappingProxyType(dict(deps or {})))
    except Exception as exc:                      # any plugin failure: phase 1 runs alone (the caller alerts)
        raise PluginError(f"{plugin}.register failed: {exc!r}") from exc
    for key, handler in proposed.items():
        if not isinstance(key, StructureType):
            raise PluginError(f"{plugin} registered key {key!r}, not a StructureType")
        if not isinstance(handler, StructureHandler):
            raise PluginError(f"{plugin}: the handler for {key} does not implement StructureHandler")
        if key not in getattr(handler, "types", ()):
            raise PluginError(f"{plugin}: the handler for {key} does not list it in its types")
        if key in handlers and handler is not handlers[key]:
            raise PluginError(f"{plugin} may not replace the phase-1 handler for {key}")
    return proposed
