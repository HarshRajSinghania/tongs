"""The review detail tells the shell when the forge truncated its file list."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import suppress
from importlib.metadata import version
from pathlib import Path
from typing import cast

import httpx
import pytest
import pytest_asyncio

from tongs.cache.store import CacheStore
from tongs.config import Config
from tongs.desktop.protocol.messages import JsonObject
from tongs.desktop.protocol.server import DesktopSidecarServer
from tongs.forges.base import ForgeClient
from tongs.forges.gitlab import GitLabClient
from tongs.forges.models import ForgeHost
from tongs.plugins.desktop_registry import DesktopPluginRegistry
from tongs.scanner.repo import ForgeType
from tongs.services import ApplicationSession
from tongs.state.drafts import DraftStore

_HOST = ForgeHost(
    "gitlab.example.com", ForgeType.GITLAB, "https://gitlab.example.com/api/v4"
)
_PROJECT = "acme/widgets"
_ENCODED = "acme%2Fwidgets"
_NUMBER = 42


def _merge_request(changes_count: object) -> dict[str, object]:
    return {
        "iid": _NUMBER,
        "title": "Vendor a large dependency",
        "state": "opened",
        "draft": False,
        "source_branch": "feature/vendor",
        "target_branch": "main",
        "web_url": f"https://gitlab.example.com/{_PROJECT}/-/merge_requests/42",
        "created_at": "2026-06-01T09:00:00Z",
        "updated_at": "2026-06-02T14:30:00Z",
        "has_conflicts": False,
        "user_notes_count": 0,
        "labels": [],
        "author": {"username": "alice", "name": "Alice Dev"},
        "head_pipeline": {"id": 9001, "status": "success"},
        "description": "Vendors a dependency with many files.",
        "merge_status": "can_be_merged",
        "detailed_merge_status": "mergeable",
        "changes_count": changes_count,
        "diff_refs": {
            "base_sha": "aaa111",
            "start_sha": "bbb222",
            "head_sha": "ccc333",
        },
    }


class _GitLab:
    """A mocked GitLab that reports the change count it is given."""

    def __init__(self) -> None:
        self.changes_count: object = "1000+"

    def handle(self, request: httpx.Request) -> httpx.Response:
        raw_path = request.url.raw_path.decode("ascii").split("?", 1)[0]
        prefix = f"/api/v4/projects/{_ENCODED}/merge_requests"
        if raw_path == prefix:
            return httpx.Response(200, json=[_merge_request(self.changes_count)])
        if raw_path == f"{prefix}/{_NUMBER}":
            return httpx.Response(200, json=_merge_request(self.changes_count))
        if raw_path == f"{prefix}/{_NUMBER}/approvals":
            return httpx.Response(200, json={"approved_by": []})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")


class _Registry:
    def __init__(self, client: GitLabClient) -> None:
        self.client = client

    def active_hostnames(self) -> list[str]:
        return [_HOST.hostname]

    def get_host(self, hostname: str) -> ForgeHost | None:
        return _HOST if hostname == _HOST.hostname else None

    async def get_client(self, _hostname: str) -> ForgeClient:
        return self.client

    async def close_all(self) -> None:
        await self.client.close()


class _Writer:
    def __init__(self) -> None:
        self.frames: asyncio.Queue[dict[str, object]] = asyncio.Queue()

    def write(self, data: bytes) -> None:
        self.frames.put_nowait(json.loads(data))

    async def drain(self) -> None:
        await asyncio.sleep(0)

    async def response(self, request_id: str) -> dict[str, object]:
        while True:
            frame = await asyncio.wait_for(self.frames.get(), 5)
            if frame.get("type") == "response" and frame.get("id") == request_id:
                return frame


class _Wire:
    def __init__(self, reader: asyncio.StreamReader, writer: _Writer) -> None:
        self.reader = reader
        self.writer = writer
        self.count = 0

    async def call(self, method: str, params: JsonObject) -> dict[str, object]:
        self.count += 1
        request_id = f"request-{self.count}"
        frame = {
            "v": 1,
            "type": "request",
            "id": request_id,
            "method": method,
            "params": params,
        }
        self.reader.feed_data(json.dumps(frame).encode() + b"\n")
        response = await self.writer.response(request_id)
        assert "error" not in response, response.get("error")
        return cast(dict[str, object], response["result"])


@pytest_asyncio.fixture
async def wired(tmp_path: Path) -> AsyncIterator[tuple[_Wire, _GitLab, str]]:
    forge = _GitLab()
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(forge.handle), base_url=_HOST.api_base
    )
    session = ApplicationSession(
        config=Config(),
        cache=CacheStore(db_path=tmp_path / "cache.db"),
        draft_store=DraftStore(tmp_path / "drafts.db"),
        forge_registry=cast(object, _Registry(GitLabClient(_HOST, http))),  # type: ignore[arg-type]
    )
    server = DesktopSidecarServer(
        session=session,
        plugin_registry=DesktopPluginRegistry(
            entry_point_source=lambda _group: (), host_version="1.0"
        ),
    )
    reader = asyncio.StreamReader()
    writer = _Writer()
    running = asyncio.create_task(server.run(reader, writer))
    wire = _Wire(reader, writer)
    try:
        await wire.call(
            "handshake",
            {
                "protocol_major": 1,
                "core_version": version("tongs"),
                "capabilities": [],
            },
        )
        repository = await wire.call(
            "repositories.open",
            {"hostname": _HOST.hostname, "project_path": _PROJECT},
        )
        listed = await wire.call(
            "reviews.list",
            {"scope": "all_open", "repository": cast(str, repository["handle"])},
        )
        items = cast(list[dict[str, object]], listed["items"])
        assert len(items) == 1
        yield wire, forge, cast(str, items[0]["handle"])
        await wire.call("shutdown", {})
        await asyncio.wait_for(running, 5)
    finally:
        if not running.done():
            reader.feed_eof()
            with suppress(BaseException):
                await asyncio.wait_for(running, 5)


async def _detail(wire: _Wire, review: str) -> dict[str, object]:
    snapshot = await wire.call("reviews.get", {"review": review})
    return cast(dict[str, object], snapshot["detail"])


@pytest.mark.asyncio
async def test_a_capped_gitlab_file_list_projects_changes_overflow(
    wired: tuple[_Wire, _GitLab, str],
) -> None:
    wire, _forge, review = wired

    detail = await _detail(wire, review)

    assert detail["changes_overflow"] is True
    assert detail["changes_count"] == 1000


@pytest.mark.asyncio
async def test_a_complete_file_list_projects_changes_overflow_false(
    wired: tuple[_Wire, _GitLab, str],
) -> None:
    wire, forge, review = wired
    forge.changes_count = "3"

    detail = await _detail(wire, review)

    # The key is always stated, so the shell never has to infer it.
    assert detail["changes_overflow"] is False
    assert detail["changes_count"] == 3
