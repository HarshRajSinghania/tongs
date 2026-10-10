"""Cached lazy CI lookups and first pages read without CI (#396)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime

import pytest
import pytest_asyncio

from tongs.cache.cached_client import (
    CachedForgeClient,
    _dict_to_mr_summary,
    _mr_summary_to_dict,
)
from tongs.cache.store import CacheStore
from tongs.errors import NetworkError
from tongs.forges.models import CIStatus, ForgeHost, MRPage, MRState, MRSummary, User
from tongs.scanner.repo import ForgeType

_HOST = ForgeHost("github.com", ForgeType.GITHUB, "https://api.github.com")


def _summary(number: int, ci: CIStatus = CIStatus.SUCCESS) -> MRSummary:
    return MRSummary(
        forge_host=_HOST,
        repo_path="acme/repo",
        local_path="",
        number=number,
        title=f"PR {number}",
        author=User("alice", "Alice"),
        state=MRState.OPEN,
        is_draft=False,
        source_branch="feature",
        target_branch="main",
        ci_status=ci,
        created_at=datetime(2026, 9, 1, tzinfo=UTC),
        updated_at=datetime(2026, 9, 2, tzinfo=UTC),
        web_url=f"https://github.com/acme/repo/pull/{number}",
        head_commit_sha=f"sha-{number}",
    )


class _LazyClient:
    """Forge double that pages with or without CI and looks CI up per review."""

    supports_lazy_ci = True

    def __init__(self) -> None:
        self.pages: list[tuple[int, bool]] = []
        self.lookups: list[tuple[str, int, str]] = []
        self.status = CIStatus.FAILED
        self.error: Exception | None = None

    async def list_mrs_page(
        self,
        repo_path: str,
        state: str = "open",
        page: int = 1,
        per_page: int = 100,
        *,
        with_ci: bool = True,
    ) -> MRPage:
        self.pages.append((page, with_ci))
        ci = CIStatus.SUCCESS if with_ci else CIStatus.UNKNOWN
        return MRPage((_summary(page * 10, ci),), has_next=page < 3)

    async def get_mr_ci_status(
        self, repo_path: str, number: int, head_sha: str = ""
    ) -> CIStatus:
        self.lookups.append((repo_path, number, head_sha))
        if self.error is not None:
            raise self.error
        return self.status


@pytest_asyncio.fixture
async def cached(tmp_path) -> AsyncIterator[tuple[CachedForgeClient, _LazyClient]]:
    store = CacheStore(db_path=tmp_path / "cache.db", max_size_mb=1)
    await store.open()
    inner = _LazyClient()
    yield CachedForgeClient(inner, store, hostname="github.com"), inner  # type: ignore[arg-type]
    await store.close()


@pytest.mark.asyncio
async def test_a_first_page_without_ci_is_cached_apart_from_one_with_it(
    cached,
) -> None:
    client, inner = cached
    lazy = await client.list_mrs_page("acme/repo", with_ci=False)
    lazy_again = await client.list_mrs_page("acme/repo", with_ci=False)
    full = await client.list_mrs_page("acme/repo")
    full_again = await client.list_mrs_page("acme/repo")

    assert inner.pages == [(1, False), (1, True)]
    assert lazy_again == lazy
    assert lazy.items[0].ci_status is CIStatus.UNKNOWN
    assert lazy_again.items[0].head_commit_sha == "sha-10"
    assert full_again == full
    assert full.items[0].ci_status is CIStatus.SUCCESS


@pytest.mark.asyncio
async def test_a_later_page_drops_both_cached_first_pages(cached) -> None:
    client, inner = cached
    await client.list_mrs_page("acme/repo", with_ci=False)
    await client.list_mrs_page("acme/repo")
    await client.list_mrs_page("acme/repo", page=2, with_ci=False)
    inner.pages.clear()

    await client.list_mrs_page("acme/repo", with_ci=False)
    await client.list_mrs_page("acme/repo")

    assert inner.pages == [(1, False), (1, True)]


@pytest.mark.asyncio
async def test_a_client_without_lazy_ci_gets_no_with_ci_argument(tmp_path) -> None:
    class _Plain:
        def __init__(self) -> None:
            self.calls = 0

        async def list_mrs_page(
            self,
            repo_path: str,
            state: str = "open",
            page: int = 1,
            per_page: int = 100,
        ) -> MRPage:
            self.calls += 1
            return MRPage((_summary(1),), has_next=False)

    store = CacheStore(db_path=tmp_path / "cache.db", max_size_mb=1)
    await store.open()
    try:
        inner = _Plain()
        client = CachedForgeClient(inner, store, hostname="github.com")  # type: ignore[arg-type]
        assert client.supports_lazy_ci is False
        page = await client.list_mrs_page("acme/repo", with_ci=False)
        again = await client.list_mrs_page("acme/repo")
    finally:
        await store.close()

    # The page came with CI, so it is the same cached first page either way.
    assert page.items[0].ci_status is CIStatus.SUCCESS
    assert again == page
    assert inner.calls == 1


@pytest.mark.asyncio
async def test_ci_lookups_are_cached_per_review_and_head(cached) -> None:
    client, inner = cached
    first = await client.get_mr_ci_status("acme/repo", 7, "sha-a")
    inner.status = CIStatus.SUCCESS
    again = await client.get_mr_ci_status("acme/repo", 7, "sha-a")
    new_head = await client.get_mr_ci_status("acme/repo", 7, "sha-b")
    other = await client.get_mr_ci_status("acme/repo", 8, "sha-a")

    assert (first, again) == (CIStatus.FAILED, CIStatus.FAILED)
    assert new_head is CIStatus.SUCCESS
    assert other is CIStatus.SUCCESS
    assert inner.lookups == [
        ("acme/repo", 7, "sha-a"),
        ("acme/repo", 7, "sha-b"),
        ("acme/repo", 8, "sha-a"),
    ]


@pytest.mark.asyncio
async def test_a_fresh_lookup_reads_past_the_cache_and_stores_its_result(
    cached,
) -> None:
    client, inner = cached
    await client.get_mr_ci_status("acme/repo", 7, "sha-a")
    inner.status = CIStatus.SUCCESS

    fresh = await client.get_mr_ci_status_fresh("acme/repo", 7, "sha-a")
    cached_after = await client.get_mr_ci_status("acme/repo", 7, "sha-a")

    assert fresh is CIStatus.SUCCESS
    assert cached_after is CIStatus.SUCCESS
    assert len(inner.lookups) == 2


@pytest.mark.asyncio
async def test_a_failed_lookup_is_not_cached(cached) -> None:
    client, inner = cached
    inner.error = NetworkError("down")
    with pytest.raises(NetworkError):
        await client.get_mr_ci_status("acme/repo", 7, "sha-a")

    inner.error = None
    assert await client.get_mr_ci_status("acme/repo", 7, "sha-a") is CIStatus.FAILED
    assert len(inner.lookups) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalidate",
    [
        lambda client: client.invalidate_review_list_reads("acme/repo"),
        lambda client: client.invalidate_repository_reads("acme/repo"),
        lambda client: client.invalidate_review_reads("acme/repo", 99),
    ],
)
async def test_dropping_review_lists_drops_cached_ci(cached, invalidate) -> None:
    client, inner = cached
    await client.get_mr_ci_status("acme/repo", 7, "sha-a")
    assert await invalidate(client) is True
    inner.status = CIStatus.RUNNING

    assert await client.get_mr_ci_status("acme/repo", 7, "sha-a") is CIStatus.RUNNING
    assert len(inner.lookups) == 2


@pytest.mark.asyncio
async def test_a_client_without_a_lookup_reports_unknown(tmp_path) -> None:
    store = CacheStore(db_path=tmp_path / "cache.db", max_size_mb=1)
    await store.open()
    try:
        client = CachedForgeClient(object(), store, hostname="github.com")  # type: ignore[arg-type]
        status = await client.get_mr_ci_status("acme/repo", 7, "sha-a")
    finally:
        await store.close()

    assert status is CIStatus.UNKNOWN


def test_rows_cached_before_the_head_commit_was_listed_still_load() -> None:
    row = _mr_summary_to_dict(_summary(7))
    del row["head_commit_sha"]

    restored = _dict_to_mr_summary(row)

    assert restored == replace(_summary(7), head_commit_sha="")


def test_the_head_commit_round_trips_through_the_cache() -> None:
    assert _dict_to_mr_summary(_mr_summary_to_dict(_summary(7))) == _summary(7)
