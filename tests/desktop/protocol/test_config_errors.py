"""Tests that the desktop handshake reports a bad config.toml as such."""

from __future__ import annotations

import asyncio
import json
from importlib.metadata import version
from pathlib import Path
from typing import cast

import pytest

from tongs.desktop.protocol.messages import JsonObject
from tongs.desktop.protocol.server import DesktopSidecarServer
from tongs.plugins.desktop_registry import DesktopPluginRegistry
from tongs.services import ApplicationSession


class _QueueWriter:
    def __init__(self) -> None:
        self.frames: asyncio.Queue[dict[str, object]] = asyncio.Queue()

    def write(self, data: bytes) -> None:
        self.frames.put_nowait(json.loads(data))

    async def drain(self) -> None:
        await asyncio.sleep(0)


def _frame(request_id: str, method: str, params: JsonObject) -> bytes:
    document = {
        "v": 1,
        "type": "request",
        "id": request_id,
        "method": method,
        "params": params,
    }
    return json.dumps(document).encode() + b"\n"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("[general]\nscan_root = ~/git\n", "line 2, column 13"),
        ('[cache]\nmr_list_ttl = "60"\n', "[cache] mr_list_ttl"),
    ],
)
@pytest.mark.asyncio
async def test_handshake_names_the_config_problem(
    tmp_path: Path, body: str, expected: str
) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(body, encoding="utf-8")
    session = ApplicationSession(
        config_path=config_file,
        cache_path=tmp_path / "cache.db",
        draft_db_path=tmp_path / "drafts.db",
    )
    server = DesktopSidecarServer(
        session=session,
        plugin_registry=DesktopPluginRegistry(
            entry_point_source=lambda _group: (), host_version="1.0"
        ),
        shutdown_timeout=0.2,
    )
    reader = asyncio.StreamReader()
    writer = _QueueWriter()
    task = asyncio.create_task(server.run(reader, writer))

    reader.feed_data(
        _frame(
            "handshake",
            "handshake",
            {
                "protocol_major": 1,
                "core_version": version("tongs"),
                "capabilities": [],
            },
        )
    )
    response = await asyncio.wait_for(writer.frames.get(), 1)
    reader.feed_eof()
    await asyncio.wait_for(task, 2)

    error = cast(JsonObject, response["error"])
    assert error["code"] == "service_error"
    details = cast(JsonObject, error["details"])
    assert details["service_code"] == "configuration_invalid"
    message = cast(str, error["message"])
    assert str(config_file) in message
    assert expected in message
    assert "forge" not in message.lower()
