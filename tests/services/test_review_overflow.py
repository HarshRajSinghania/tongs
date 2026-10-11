"""A GitLab MR whose changes_count overflows still opens through the session."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import cast

import httpx
import pytest
import pytest_asyncio

from tongs.cache.cached_client import CachedForgeClient
from tongs.cache.store import CacheStore
from tongs.config import Config
from tongs.forges.base import ForgeClient
from tongs.forges.gitlab import GitLabClient
from tongs.forges.models import ForgeHost
from tongs.scanner.repo import ForgeType
from tongs.services import ApplicationSession, RepositoryRef, ReviewRef
from tongs.state.drafts import DraftStore

_HOST = ForgeHost(
    "gitlab.example.com", ForgeType.GITLAB, "https://gitlab.example.com/api/v4"
)
_PROJECT = "acme/widgets"
_ENCODED = "acme%2Fwidgets"
_NUMBER = 42
_FILES_RETURNED = 3

_ClientFactory = Callable[[ForgeClient, CacheStore], ForgeClient]


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


def _change(index: int) -> dict[str, object]:
    path = f"vendor/file_{index}.py"
    return {
        "old_path": path,
        "new_path": path,
        "new_file": True,
        "renamed_file": False,
        "deleted_file": False,
        "diff": "@@ -0,0 +1 @@\n+print('hello')\n",
    }


class _GitLab:
    """A mocked GitLab that reports a capped ``1000+`` change list."""

    def __init__(self, changes_count: object) -> None:
        self.changes_count = changes_count
        self.paths: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        raw_path = request.url.raw_path.decode("ascii").split("?", 1)[0]
        self.paths.append(raw_path)
        prefix = f"/api/v4/projects/{_ENCODED}/merge_requests"
        if raw_path == prefix:
            return httpx.Response(200, json=[_merge_request(self.changes_count)])
        if raw_path == f"{prefix}/{_NUMBER}":
            return httpx.Response(200, json=_merge_request(self.changes_count))
        if raw_path == f"{prefix}/{_NUMBER}/approvals":
            return httpx.Response(200, json={"approved_by": []})
        if raw_path == f"{prefix}/{_NUMBER}/diffs":
            page = int(request.url.params.get("page", "1"))
            files = [_change(index) for index in range(_FILES_RETURNED)]
            return httpx.Response(200, json=files if page == 1 else [])
        raise AssertionError(f"unexpected request: {request.method} {request.url}")


class _Registry:
    def __init__(self, client: ForgeClient) -> None:
        self.client = client

    def active_hostnames(self) -> list[str]:
        return [_HOST.hostname]

    def get_host(self, hostname: str) -> ForgeHost | None:
        return _HOST if hostname == _HOST.hostname else None

    async def get_client(self, _hostname: str) -> ForgeClient:
        return self.client

    async def close_all(self) -> None:
        await self.client.close()


def _direct(client: ForgeClient, _cache: CacheStore) -> ForgeClient:
    return client


def _cached(client: ForgeClient, cache: CacheStore) -> ForgeClient:
    return CachedForgeClient(inner=client, cache=cache, hostname=_HOST.hostname)


@pytest_asyncio.fixture(params=[_direct, _cached], ids=["direct", "cached"])
async def opened(
    request: pytest.FixtureRequest, tmp_path: Path
) -> AsyncIterator[tuple[ApplicationSession, ReviewRef, _GitLab]]:
    forge = _GitLab("1000+")
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(forge.handle), base_url=_HOST.api_base
    )
    cache = CacheStore(db_path=tmp_path / "cache.db")
    factory = cast(_ClientFactory, request.param)
    client = factory(GitLabClient(_HOST, http), cache)
    session = ApplicationSession(
        config=Config(),
        cache=cache,
        draft_store=DraftStore(tmp_path / "drafts.db"),
        forge_registry=cast(object, _Registry(client)),  # type: ignore[arg-type]
    )
    await session.start()
    try:
        repository = await session.open_repository(_HOST.hostname, _PROJECT)
        assert repository.ref == RepositoryRef(_HOST.hostname, _PROJECT)
        yield session, ReviewRef(repository.ref, _NUMBER), forge
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_get_review_opens_an_overflowed_gitlab_mr(
    opened: tuple[ApplicationSession, ReviewRef, _GitLab],
) -> None:
    session, ref, _forge = opened

    snapshot = await session.get_review(ref)

    assert snapshot.revision_error is None
    assert snapshot.revision is not None
    assert snapshot.revision.head_sha == "ccc333"
    assert snapshot.detail.changes_count == 1000
    assert snapshot.detail.changes_overflow is True


@pytest.mark.asyncio
async def test_get_raw_diff_opens_an_overflowed_gitlab_mr(
    opened: tuple[ApplicationSession, ReviewRef, _GitLab],
) -> None:
    session, ref, forge = opened

    diff = await session.get_raw_diff(ref)

    assert diff.revision.head_sha == "ccc333"
    assert diff.revision.start_sha == "bbb222"
    assert [change["new_path"] for change in diff.changes] == [
        f"vendor/file_{index}.py" for index in range(_FILES_RETURNED)
    ]
    assert any(path.endswith("/diffs") for path in forge.paths)


@pytest.mark.asyncio
async def test_complete_gitlab_mr_does_not_report_overflow(
    opened: tuple[ApplicationSession, ReviewRef, _GitLab],
) -> None:
    session, ref, forge = opened
    forge.changes_count = "3"

    snapshot = await session.get_review(ref)

    assert snapshot.detail.changes_count == 3
    assert snapshot.detail.changes_overflow is False
