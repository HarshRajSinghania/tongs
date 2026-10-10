"""Tests for the built-in MCP terminal plugin."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tongs.mcp import plugin
from tongs.plugins.context import PluginContext


@pytest.fixture
def available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(plugin, "_mcp_available", lambda: True)


@pytest.fixture
def no_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the MCP plugin must not start a process")

    monkeypatch.setattr(subprocess, "Popen", refuse)


async def _attached(app: MagicMock) -> plugin.MCPPlugin:
    mcp_plugin = plugin.MCPPlugin()
    await mcp_plugin.on_app_ready(PluginContext(app))
    return mcp_plugin


def test_the_palette_offers_a_setup_help_command(available: None) -> None:
    mcp_plugin = plugin.MCPPlugin()

    ((display, help_text, callback),) = mcp_plugin.get_commands()

    assert display == "MCP Server Setup"
    assert help_text == "Show how to register tongs-mcp with an MCP client"
    assert callback == mcp_plugin._show_setup


def test_the_command_is_hidden_without_the_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(plugin, "_mcp_available", lambda: False)

    assert plugin.MCPPlugin().get_commands() == []


def test_the_installed_extra_is_detected() -> None:
    assert plugin._mcp_available() is True


@pytest.mark.asyncio
async def test_setup_shows_the_registration_command_without_a_process(
    available: None, no_spawn: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(plugin, "_server_command", lambda: "tongs-mcp")
    app = MagicMock()
    mcp_plugin = await _attached(app)

    ((_display, _help, callback),) = mcp_plugin.get_commands()
    callback()

    app.notify.assert_called_once_with(
        "MCP clients start tongs-mcp themselves. Register it once, "
        "for example: claude mcp add tongs -- tongs-mcp",
        severity="information",
    )


@pytest.mark.asyncio
async def test_setup_escapes_markup_in_an_install_path(
    available: None, no_spawn: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(plugin, "_server_command", lambda: "/opt/[x]/tongs-mcp")
    app = MagicMock()
    mcp_plugin = await _attached(app)

    mcp_plugin._show_setup()

    message = app.notify.call_args.args[0]
    assert message.endswith(r"claude mcp add tongs -- /opt/\[x]/tongs-mcp")


@pytest.mark.asyncio
async def test_setup_warns_when_the_extra_disappears(
    monkeypatch: pytest.MonkeyPatch, no_spawn: None
) -> None:
    monkeypatch.setattr(plugin, "_mcp_available", lambda: False)
    app = MagicMock()
    mcp_plugin = await _attached(app)

    mcp_plugin._show_setup()

    app.notify.assert_called_once_with(
        r'Install the MCP extra first: pipx install "tongs\[mcp]"',
        severity="warning",
    )


@pytest.mark.asyncio
async def test_setup_before_the_app_is_ready_raises_for_the_palette_guard(
    available: None,
) -> None:
    mcp_plugin = plugin.MCPPlugin()

    with pytest.raises(RuntimeError, match="not attached"):
        mcp_plugin._show_setup()

    await mcp_plugin.on_app_ready(PluginContext(MagicMock()))
    await mcp_plugin.on_app_shutdown(PluginContext(MagicMock()))
    with pytest.raises(RuntimeError, match="not attached"):
        mcp_plugin._show_setup()


def _fake_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "install dir" / "bin"
    bin_dir.mkdir(parents=True)
    executable = bin_dir / "tongs-mcp"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setattr(sys, "executable", str(bin_dir / "python"))
    return executable


def test_server_command_is_bare_when_path_finds_this_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = _fake_install(tmp_path, monkeypatch)
    link_dir = tmp_path / "local-bin"
    link_dir.mkdir()
    (link_dir / "tongs-mcp").symlink_to(executable)
    monkeypatch.setenv("PATH", str(link_dir))

    assert plugin._server_command() == "tongs-mcp"


def test_server_command_is_absolute_when_path_finds_another_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = _fake_install(tmp_path, monkeypatch)
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    other = other_dir / "tongs-mcp"
    other.write_text("#!/bin/sh\n", encoding="utf-8")
    other.chmod(0o755)
    monkeypatch.setenv("PATH", str(other_dir))

    command = plugin._server_command()

    assert shlex.split(command) == [str(executable)]
    assert plugin.registration_command() == f"claude mcp add tongs -- {command}"


def test_server_command_is_absolute_when_path_has_no_tongs_mcp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = _fake_install(tmp_path, monkeypatch)
    monkeypatch.setenv("PATH", os.devnull)

    assert shlex.split(plugin._server_command()) == [str(executable)]


def test_server_command_falls_back_to_the_bare_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python"))

    assert plugin._server_command() == "tongs-mcp"
