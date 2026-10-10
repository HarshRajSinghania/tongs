"""Plugin discovery and lifecycle management."""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable, Coroutine
from importlib.metadata import entry_points
from typing import TYPE_CHECKING, Any

from rich.markup import escape

from tongs.plugins.base import TongsPlugin
from tongs.plugins.context import PluginContext

if TYPE_CHECKING:
    from textual.app import App

log = logging.getLogger(__name__)


class PluginRegistry:
    """Discovers and manages tongs plugins."""

    def __init__(self) -> None:
        self._plugins: list[TongsPlugin] = []

    @property
    def plugins(self) -> list[TongsPlugin]:
        return list(self._plugins)

    def discover(self, plugin_config: dict[str, dict] | None = None) -> None:
        """Discover plugins via entry points, filtered by config."""
        config = plugin_config or {}
        eps = entry_points(group="tongs.plugins")
        for ep in eps:
            plugin_conf = config.get(ep.name, {})
            if not plugin_conf.get("enabled", True):
                log.info("Plugin %s disabled by config", ep.name)
                continue
            try:
                plugin_cls = ep.load()
                plugin = plugin_cls()
                if not isinstance(plugin, TongsPlugin):
                    log.warning(
                        "Plugin %s is not a TongsPlugin subclass, skipping",
                        ep.name,
                    )
                    continue
                self._plugins.append(plugin)
                log.info("Loaded plugin: %s v%s", plugin.name, plugin.version)
            except Exception:
                log.warning("Failed to load plugin %s", ep.name, exc_info=True)

    async def on_app_ready(self, app) -> None:
        ctx = PluginContext(app)
        for plugin in self._plugins:
            try:
                await plugin.on_app_ready(ctx)
            except Exception:
                log.warning("Plugin %s.on_app_ready failed", plugin.name, exc_info=True)

    async def on_app_shutdown(self, app) -> None:
        ctx = PluginContext(app)
        for plugin in self._plugins:
            try:
                await plugin.on_app_shutdown(ctx)
            except Exception:
                log.warning(
                    "Plugin %s.on_app_shutdown failed",
                    plugin.name,
                    exc_info=True,
                )

    def get_all_commands(self, app: App | None = None) -> list[tuple[str, str, object]]:
        """Return every plugin palette command with its callback guarded.

        A callback that raises, synchronously or when awaited, is logged with
        the plugin name and, when ``app`` is given, reported with an error
        notification instead of reaching the Textual message loop.
        """
        commands: list[tuple[str, str, object]] = []
        for plugin in self._plugins:
            try:
                guarded = [
                    (
                        display,
                        help_text,
                        _guarded_callback(plugin.name, display, callback, app),
                    )
                    for display, help_text, callback in plugin.get_commands()
                ]
            except Exception:
                log.warning("Plugin %s.get_commands failed", plugin.name, exc_info=True)
                continue
            commands.extend(guarded)
        return commands

    def get_all_screens(self) -> dict[str, type]:
        screens: dict[str, type] = {}
        for plugin in self._plugins:
            try:
                screens.update(plugin.get_screens())
            except Exception:
                log.warning("Plugin %s.get_screens failed", plugin.name, exc_info=True)
        return screens


def _guarded_callback(
    plugin_name: str, display: object, callback: object, app: App | None
) -> Callable[[], Coroutine[Any, Any, None]]:
    """Wrap one plugin palette callback so its failure stays isolated."""

    async def run_guarded() -> None:
        try:
            result = callback()  # type: ignore[operator]
            if inspect.isawaitable(result):
                await result
        except Exception:
            log.warning(
                "Plugin %s command %r failed", plugin_name, display, exc_info=True
            )
            if app is not None:
                app.notify(
                    f"The {escape(plugin_name)} plugin command failed. See the log.",
                    severity="error",
                )

    return run_guarded
