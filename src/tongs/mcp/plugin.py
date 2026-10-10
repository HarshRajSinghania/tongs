"""MCP server plugin for tongs."""

from __future__ import annotations

import os
import shlex
import shutil
import sys
from importlib.util import find_spec
from pathlib import Path

from rich.markup import escape

from tongs.plugins.base import TongsPlugin
from tongs.plugins.context import PluginContext


def _mcp_available() -> bool:
    """Return whether the optional MCP server dependency can be imported."""
    try:
        return find_spec("mcp.server.fastmcp") is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def _server_command() -> str:
    """Return the shell word that runs this install's ``tongs-mcp``.

    The bare name is used when ``PATH`` resolves to this install's executable.
    Otherwise the executable next to the running interpreter is named by its
    absolute path, so the registration starts the same install.
    """
    sibling = Path(sys.executable).with_name("tongs-mcp")
    if not sibling.is_file():
        return "tongs-mcp"
    on_path = shutil.which("tongs-mcp")
    if on_path is not None:
        try:
            if os.path.samefile(on_path, sibling):
                return "tongs-mcp"
        except OSError:
            pass
    return shlex.quote(str(sibling))


def registration_command() -> str:
    """Return the command that registers the tongs MCP server with Claude Code."""
    return f"claude mcp add tongs -- {_server_command()}"


class MCPPlugin(TongsPlugin):
    """Shows how to register the tongs MCP server from the command palette.

    MCP clients start ``tongs-mcp`` themselves over stdio, so the terminal app
    never launches the server. The palette command only explains the setup.
    """

    def __init__(self) -> None:
        self._context: PluginContext | None = None

    @property
    def name(self) -> str:
        return "mcp"

    @property
    def version(self) -> str:
        return "0.3.0"

    async def on_app_ready(self, ctx: PluginContext) -> None:
        self._context = ctx

    async def on_app_shutdown(self, ctx: PluginContext) -> None:
        self._context = None

    def get_commands(self) -> list[tuple[str, str, object]]:
        if not _mcp_available():
            return []
        return [
            (
                "MCP Server Setup",
                "Show how to register tongs-mcp with an MCP client",
                self._show_setup,
            ),
        ]

    def _show_setup(self) -> None:
        if self._context is None:
            raise RuntimeError("the MCP plugin is not attached to the app")
        if _mcp_available():
            message = (
                "MCP clients start tongs-mcp themselves. Register it once, "
                f"for example: {registration_command()}"
            )
            severity = "information"
        else:
            message = 'Install the MCP extra first: pipx install "tongs[mcp]"'
            severity = "warning"
        # Notices render as markup, and "[mcp]" or an install path is not markup.
        self._context.notify(escape(message), severity=severity)
