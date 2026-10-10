"""A diff layout switch re-projects the read on screen without a forge request."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import cast

import httpx
import pytest
import pytest_asyncio

from tongs.cache.store import CacheStore
from tongs.config import Config
from tongs.desktop.protocol.diff_projection import ConvertedDiffCache
from tongs.desktop.protocol.messages import JsonObject
from tongs.desktop.protocol.server import DesktopSidecarServer
from tongs.diff.models import DiffFile, DiffHunk, DiffLine, FileStatus, LineType
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
_PREFIX = f"/api/v4/projects/{_ENCODED}/merge_requests"


def _merge_request(head_sha: str) -> dict[str, object]:
    return {
        "iid": _NUMBER,
        "title": "Tidy the widget loader",
        "state": "opened",
        "draft": False,
        "source_branch": "feature/loader",
        "target_branch": "main",
        "web_url": f"https://gitlab.example.com/{_PROJECT}/-/merge_requests/42",
        "created_at": "2026-06-01T09:00:00Z",
        "updated_at": "2026-06-02T14:30:00Z",
        "has_conflicts": False,
        "user_notes_count": 0,
        "labels": [],
        "author": {"username": "alice", "name": "Alice Dev"},
        "head_pipeline": {"id": 9001, "status": "success"},
        "description": "",
        "merge_status": "can_be_merged",
        "detailed_merge_status": "mergeable",
        "changes_count": "1",
        "diff_refs": {
            "base_sha": "aaa111",
            "start_sha": "bbb222",
            "head_sha": head_sha,
        },
    }


class _GitLab:
    """A mocked GitLab that records every request the session sends it."""

    def __init__(self) -> None:
        self.head_sha = "ccc333"
        self.content = "new"
        self.requests: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        raw_path = request.url.raw_path.decode("ascii").split("?", 1)[0]
        self.requests.append(raw_path)
        if raw_path == _PREFIX:
            return httpx.Response(200, json=[_merge_request(self.head_sha)])
        if raw_path == f"{_PREFIX}/{_NUMBER}":
            return httpx.Response(200, json=_merge_request(self.head_sha))
        if raw_path == f"{_PREFIX}/{_NUMBER}/changes":
            return httpx.Response(
                200,
                json={
                    "changes": [
                        {
                            "old_path": "loader.py",
                            "new_path": "loader.py",
                            "diff": f"@@ -1,2 +1,2 @@\n same\n-old\n+{self.content}\n",
                        }
                    ]
                },
            )
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


@dataclass(frozen=True)
class _Wired:
    wire: _Wire
    forge: _GitLab
    review: str


@pytest_asyncio.fixture
async def wired(tmp_path: Path) -> AsyncIterator[_Wired]:
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
        yield _Wired(wire, forge, cast(str, items[0]["handle"]))
        await wire.call("shutdown", {})
        await asyncio.wait_for(running, 5)
    finally:
        if not running.done():
            reader.feed_eof()
            with suppress(BaseException):
                await asyncio.wait_for(running, 5)


def _kinds(page: dict[str, object]) -> list[str]:
    return [
        cast(str, entry["kind"])
        for entry in cast(list[dict[str, object]], page["entries"])
    ]


def _contents(page: dict[str, object]) -> list[str]:
    contents: list[str] = []
    for entry in cast(list[dict[str, object]], page["entries"]):
        if entry["kind"] == "line":
            contents.append(cast(str, entry["content"]))
        elif entry["kind"] == "split":
            for side in ("old", "new"):
                cell = entry[side]
                if isinstance(cell, dict):
                    contents.append(cast(str, cell["content"]))
    return contents


@pytest.mark.asyncio
async def test_layout_switch_reuses_the_open_read_with_no_forge_request(
    wired: _Wired,
) -> None:
    unified = await wired.wire.call(
        "diff.open", {"review": wired.review, "layout": "unified"}
    )
    reads_after_open = len(wired.forge.requests)
    assert wired.forge.requests.count(f"{_PREFIX}/{_NUMBER}/changes") == 1

    split = await wired.wire.call(
        "diff.open",
        {
            "review": wired.review,
            "layout": "split",
            "from_snapshot": cast(str, unified["snapshot_id"]),
        },
    )
    back = await wired.wire.call(
        "diff.open",
        {
            "review": wired.review,
            "layout": "unified",
            "from_snapshot": cast(str, split["snapshot_id"]),
        },
    )

    assert len(wired.forge.requests) == reads_after_open
    assert _kinds(split) == ["file", "hunk", "split", "split"]
    assert _kinds(back) == _kinds(unified)
    assert split["revision"] == unified["revision"] == back["revision"]
    assert split["snapshot_id"] != unified["snapshot_id"]
    assert back["snapshot_id"] != split["snapshot_id"]


@pytest.mark.asyncio
async def test_open_without_a_snapshot_reads_the_forge_and_drops_the_old_read(
    wired: _Wired,
) -> None:
    first = await wired.wire.call(
        "diff.open", {"review": wired.review, "layout": "unified"}
    )
    wired.forge.head_sha = "ddd444"
    wired.forge.content = "newer"

    refreshed = await wired.wire.call(
        "diff.open", {"review": wired.review, "layout": "unified"}
    )
    reads_after_refresh = len(wired.forge.requests)
    stale = await wired.wire.call(
        "diff.open",
        {
            "review": wired.review,
            "layout": "split",
            "from_snapshot": cast(str, first["snapshot_id"]),
        },
    )

    assert wired.forge.requests.count(f"{_PREFIX}/{_NUMBER}/changes") == 3
    assert len(wired.forge.requests) > reads_after_refresh
    assert cast(JsonObject, refreshed["revision"])["head_sha"] == "ddd444"
    assert cast(JsonObject, stale["revision"])["head_sha"] == "ddd444"
    assert "newer" in _contents(stale)


@pytest.mark.asyncio
async def test_an_unknown_snapshot_falls_back_to_a_forge_read(
    wired: _Wired,
) -> None:
    await wired.wire.call("diff.open", {"review": wired.review})
    reads_after_open = len(wired.forge.requests)

    page = await wired.wire.call(
        "diff.open",
        {"review": wired.review, "layout": "split", "from_snapshot": "unknown-1"},
    )

    assert len(wired.forge.requests) > reads_after_open
    assert _kinds(page) == ["file", "hunk", "split", "split"]


def _files(lines: int) -> tuple[DiffFile, ...]:
    hunk = DiffHunk(
        "@@ -1 +1 @@",
        1,
        lines,
        1,
        lines,
        tuple(
            DiffLine(index + 1, index + 1, "same", LineType.CONTEXT)
            for index in range(lines)
        ),
    )
    return (DiffFile("a.py", "a.py", FileStatus.MODIFIED, (hunk,)),)


_REVISION: JsonObject = {"head_sha": "h", "base_sha": "b", "start_sha": None}


def test_cache_reuses_only_snapshots_projected_from_the_same_read() -> None:
    cache = ConvertedDiffCache()
    files = _files(2)
    cache.store("review", "s1", _REVISION, files)
    cache.remember("review", "s2")

    assert cache.lookup("review", "s1") is not None
    reused = cache.lookup("review", "s2")
    assert reused is not None
    assert reused.files is files
    assert reused.revision == _REVISION
    assert cache.lookup("review", "other") is None
    assert cache.lookup("another-review", "s1") is None

    cache.store("review", "s3", _REVISION, files)
    assert cache.lookup("review", "s1") is None
    assert cache.lookup("review", "s3") is not None
    cache.drop("review")
    assert cache.lookup("review", "s3") is None


def test_cache_is_bounded_by_reviews_lines_and_snapshot_ids() -> None:
    cache = ConvertedDiffCache(max_reviews=2, max_lines=20, max_snapshots_per_review=2)
    cache.store("a", "a1", _REVISION, _files(4))
    cache.store("b", "b1", _REVISION, _files(4))
    assert cache.lookup("a", "a1") is not None
    cache.store("c", "c1", _REVISION, _files(4))
    # "b" was used least recently, so it gave way to "c".
    assert cache.lookup("b", "b1") is None
    assert cache.lookup("a", "a1") is not None
    assert cache.lookup("c", "c1") is not None

    # 4 lines, 1 hunk header and 1 file row make 6 per entry; 17 more would
    # pass 20 with either neighbour, so both make way.
    cache.store("d", "d1", _REVISION, _files(15))
    assert cache.lookup("a", "a1") is None
    assert cache.lookup("c", "c1") is None
    assert cache.lookup("d", "d1") is not None

    # A diff larger than the whole bound is not kept at all.
    cache.store("e", "e1", _REVISION, _files(30))
    assert cache.lookup("e", "e1") is None
    assert cache.lookup("d", "d1") is not None

    cache.remember("d", "d2")
    cache.remember("d", "d3")
    assert cache.lookup("d", "d1") is None
    assert cache.lookup("d", "d3") is not None

    cache.clear()
    assert cache.lookup("d", "d3") is None


def test_cache_rejects_non_positive_limits() -> None:
    with pytest.raises(ValueError):
        ConvertedDiffCache(max_reviews=0)
