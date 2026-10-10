"""A cached review read never outlives a mutation that ran during it (#309)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime

import pytest
import pytest_asyncio

from tongs.cache.cached_client import CachedForgeClient
from tongs.cache.store import CacheStore
from tongs.forges.base import ForgeClient
from tongs.forges.models import (
    CIStatus,
    ForgeHost,
    ForgeMergeResult,
    MRPage,
    MRState,
    MRSummary,
    User,
)
from tongs.scanner.repo import ForgeType

_HOST = ForgeHost("github.com", ForgeType.GITHUB, "https://api.github.com")


def _summary(number: int = 1, *, state: MRState = MRState.OPEN) -> MRSummary:
    return MRSummary(
        forge_host=_HOST,
        repo_path="acme/repo",
        local_path="",
        number=number,
        title=f"PR {number}",
        author=User("alice", "Alice"),
        state=state,
        is_draft=False,
        source_branch="feature",
        target_branch="main",
        ci_status=CIStatus.SUCCESS,
        created_at=datetime(2026, 9, 1, tzinfo=UTC),
        updated_at=datetime(2026, 9, 2, tzinfo=UTC),
        web_url=f"https://github.com/acme/repo/pull/{number}",
    )


class _Forge:
    """Inner client whose reads can be held open while a merge lands."""

    def __init__(self) -> None:
        self.merged = False
        self.hold: asyncio.Event | None = None
        self.started = asyncio.Event()
        self.reads: list[str] = []

    async def _read(self, name: str) -> bool:
        self.reads.append(name)
        # The forge answers with the state at the moment the read arrived.
        merged = self.merged
        self.started.set()
        if self.hold is not None:
            await self.hold.wait()
        return merged

    def _items(self, merged: bool) -> list[MRSummary]:
        return [] if merged else [_summary()]

    async def list_mrs(
        self, repo_path: str, state: str = "open", per_page: int = 100
    ) -> list[MRSummary]:
        return self._items(await self._read("list"))

    async def list_mrs_page(
        self, repo_path: str, state: str = "open", page: int = 1, per_page: int = 100
    ) -> MRPage:
        return MRPage(tuple(self._items(await self._read("page"))), has_next=False)

    async def get_mr_diff(self, repo_path: str, number: int) -> list[dict]:
        merged = await self._read("diff")
        return [{"new_path": "after.py" if merged else "before.py"}]

    async def merge_mr(
        self,
        repo_path: str,
        number: int,
        squash: bool = False,
        delete_branch: bool = True,
    ) -> ForgeMergeResult:
        self.merged = True
        return ForgeMergeResult("merge-1", "sha-1")

    async def close(self) -> None:
        return None


@pytest_asyncio.fixture
async def store(tmp_path) -> AsyncIterator[CacheStore]:
    cache = CacheStore(db_path=tmp_path / "cache.db", max_size_mb=1)
    await cache.open()
    yield cache
    await cache.close()


def _client(forge: object, store: CacheStore) -> CachedForgeClient:
    return CachedForgeClient(forge, store, hostname="github.com")  # type: ignore[arg-type]


async def _cached_keys(store: CacheStore) -> set[str]:
    assert store._db is not None
    cursor = await store._db.execute("SELECT key FROM cache")
    return {row[0] for row in await cursor.fetchall()}


async def _drain(client: CachedForgeClient) -> None:
    tasks = list(client._invalidation_tasks.values())
    if tasks:
        await asyncio.gather(*tasks)


_READS = {
    "list": lambda client: client.list_mrs("acme/repo"),
    "page": lambda client: client.list_mrs_page("acme/repo", page=1),
    "diff": lambda client: client.get_mr_diff("acme/repo", 1),
}


def _is_pre_merge(name: str, result: object) -> bool:
    if name == "list":
        return bool(result)
    if name == "page":
        assert isinstance(result, MRPage)
        return bool(result.items)
    return result == [{"new_path": "before.py"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(_READS))
async def test_a_slow_read_interleaved_with_a_merge_is_not_stored(
    store: CacheStore, name: str
) -> None:
    forge = _Forge()
    client = _client(forge, store)
    forge.hold = asyncio.Event()
    read = asyncio.create_task(_READS[name](client))
    await forge.started.wait()

    # The merge lands, and its invalidation finishes, while the read is held.
    await client.merge_mr("acme/repo", 1)
    await _drain(client)
    assert client._dirty_review_prefixes == {}
    forge.hold.set()
    stale = await read

    # The caller still gets what it asked for, but the cache never keeps it.
    assert _is_pre_merge(name, stale)
    assert await _cached_keys(store) == set()
    forge.hold = None
    fresh = await _READS[name](client)
    assert not _is_pre_merge(name, fresh)
    assert forge.reads == [name, name]
    again = await _READS[name](client)
    assert again == fresh
    assert forge.reads == [name, name]


@pytest.mark.asyncio
async def test_a_slow_read_interleaved_with_a_repository_invalidation_is_not_stored(
    store: CacheStore,
) -> None:
    forge = _Forge()
    client = _client(forge, store)
    forge.hold = asyncio.Event()
    read = asyncio.create_task(client.list_mrs_page("acme/repo", page=1))
    await forge.started.wait()

    assert await client.invalidate_repository_reads("acme/repo") is True
    forge.hold.set()
    await read

    assert await _cached_keys(store) == set()


@pytest.mark.asyncio
async def test_repository_invalidation_drops_lists_and_keeps_other_repositories(
    store: CacheStore,
) -> None:
    forge = _Forge()
    client = _client(forge, store)
    await client.list_mrs("acme/repo")
    await client.list_mrs_page("acme/repo", page=1)
    await client.list_mrs("acme/repo-docs")
    await client.list_mrs("acme/repo_x")

    assert await client.invalidate_repository_reads("acme/repo") is True

    assert await _cached_keys(store) == {
        "github.com:acme/repo-docs:mrs:open",
        "github.com:acme/repo_x:mrs:open",
    }
    assert client._dirty_review_prefixes == {}
    await client.list_mrs("acme/repo")
    await client.list_mrs_page("acme/repo", page=1)
    assert forge.reads[-2:] == ["list", "page"]


@pytest.mark.asyncio
async def test_a_failed_repository_invalidation_keeps_reads_away_from_the_cache(
    store: CacheStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    forge = _Forge()
    client = _client(forge, store)
    await client.list_mrs("acme/repo")

    async def broken(_prefix: str) -> None:
        raise RuntimeError("disk failure")

    monkeypatch.setattr(store, "invalidate_prefix", broken)
    assert await client.invalidate_repository_reads("acme/repo") is False
    forge.merged = True

    # The stale row is still on disk, but this process does not serve it.
    assert await client.list_mrs("acme/repo") == []
    assert forge.reads == ["list", "list"]


@pytest.mark.asyncio
async def test_a_client_without_a_cache_reports_nothing_invalidated() -> None:
    # The base implementation needs no client state, so any instance will do.
    dropped = await ForgeClient.invalidate_repository_reads(
        object(),  # type: ignore[arg-type]
        "acme/repo",
    )
    assert dropped is False


@pytest.mark.asyncio
async def test_invalidating_mr_1_keeps_mr_12_cached(
    store: CacheStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    forge = _Forge()
    client = _client(forge, store)
    await client.get_mr_diff("acme/repo", 1)
    await client.get_mr_diff("acme/repo", 12)
    # Hold the store invalidation so MR 1 stays marked dirty in memory.
    release = asyncio.Event()
    invalidate_prefix = store.invalidate_prefix

    async def held(prefix: str) -> None:
        await release.wait()
        await invalidate_prefix(prefix)

    monkeypatch.setattr(store, "invalidate_prefix", held)
    invalidation = asyncio.create_task(client.invalidate_review_reads("acme/repo", 1))
    await asyncio.sleep(0)
    assert "github.com:acme/repo:mr:1" in client._dirty_review_prefixes

    await client.get_mr_diff("acme/repo", 12)
    await client.get_mr_diff("acme/repo", 1)
    assert forge.reads == ["diff", "diff", "diff"]

    release.set()
    assert await invalidation is True
    assert "github.com:acme/repo:mr:12:diff" in await _cached_keys(store)
    assert "github.com:acme/repo:mr:1:diff" not in await _cached_keys(store)


@pytest.mark.asyncio
async def test_has_conflicts_survives_the_cache(store: CacheStore) -> None:
    conflicted = replace(_summary(), has_conflicts=True)

    class Conflicted(_Forge):
        async def list_mrs(
            self, repo_path: str, state: str = "open", per_page: int = 100
        ) -> list[MRSummary]:
            await self._read("list")
            return [conflicted]

        async def list_mrs_page(
            self,
            repo_path: str,
            state: str = "open",
            page: int = 1,
            per_page: int = 100,
        ) -> MRPage:
            await self._read("page")
            return MRPage((conflicted,), has_next=False)

    forge = Conflicted()
    client = _client(forge, store)
    await client.list_mrs("acme/repo")
    await client.list_mrs_page("acme/repo", page=1)

    listed = await client.list_mrs("acme/repo")
    paged = await client.list_mrs_page("acme/repo", page=1)

    assert forge.reads == ["list", "page"]
    assert listed == [conflicted]
    assert paged.items == (conflicted,)
    assert listed[0].has_conflicts is True


@pytest.mark.asyncio
async def test_a_mutation_elsewhere_does_not_stop_a_read_being_stored(
    store: CacheStore,
) -> None:
    forge = _Forge()
    client = _client(forge, store)
    forge.hold = asyncio.Event()
    read = asyncio.create_task(client.list_mrs_page("acme/repo", page=1))
    await forge.started.wait()

    # Concurrent refreshes and merges of other repositories, and of a sibling
    # whose name only starts with this one, leave this read's result valid.
    assert await client.invalidate_repository_reads("acme/other") is True
    assert await client.invalidate_repository_reads("acme/repo-docs") is True
    await client.merge_mr("acme/repo_x", 3)
    await _drain(client)
    forge.hold.set()
    await read

    assert await _cached_keys(store) == {"github.com:acme/repo:mrs:open:page:100:1"}


@pytest.mark.asyncio
async def test_review_list_invalidation_drops_lists_and_keeps_diffs(
    store: CacheStore,
) -> None:
    forge = _Forge()
    client = _client(forge, store)
    await client.list_mrs("acme/repo")
    await client.list_mrs_page("acme/repo", page=1)
    await client.get_mr_diff("acme/repo", 1)

    assert await client.invalidate_review_list_reads("acme/repo") is True

    assert await _cached_keys(store) == {"github.com:acme/repo:mr:1:diff"}
    assert client._dirty_review_prefixes == {}
    await client.get_mr_diff("acme/repo", 1)
    assert forge.reads == ["list", "page", "diff"]


@pytest.mark.asyncio
async def test_review_list_invalidation_lets_a_diff_read_in_flight_be_stored(
    store: CacheStore,
) -> None:
    forge = _Forge()
    client = _client(forge, store)
    forge.hold = asyncio.Event()
    diff = asyncio.create_task(client.get_mr_diff("acme/repo", 1))
    page = asyncio.create_task(client.list_mrs_page("acme/repo", page=1))
    while len(forge.reads) < 2:
        await asyncio.sleep(0)

    assert await client.invalidate_review_list_reads("acme/repo") is True
    forge.hold.set()
    await asyncio.gather(diff, page)

    # The list read may predate the refresh; the diff read is unaffected.
    assert await _cached_keys(store) == {"github.com:acme/repo:mr:1:diff"}


@pytest.mark.asyncio
async def test_invalidation_marks_stay_bounded_without_reads_in_flight(
    store: CacheStore,
) -> None:
    forge = _Forge()
    client = _client(forge, store)
    for number in range(500):
        assert await client.invalidate_review_reads("acme/repo", number) is True
        assert await client.invalidate_repository_reads(f"acme/repo-{number}") is True
        await client.list_mrs("acme/repo")

    assert len(client._marked_generations) == 0
    assert client._read_generations == {}


@pytest.mark.asyncio
async def test_invalidation_marks_are_kept_only_while_an_older_read_is_in_flight(
    store: CacheStore,
) -> None:
    forge = _Forge()
    client = _client(forge, store)
    forge.hold = asyncio.Event()
    read = asyncio.create_task(client.get_mr_diff("acme/repo", 1))
    await forge.started.wait()

    for number in range(2, 202):
        await client.invalidate_review_reads("acme/repo", number)
    await client.invalidate_review_reads("acme/repo", 1)
    # One prefix per review plus the shared list prefix, all newer than the
    # read in flight.
    assert len(client._marked_generations) == 201 + 1
    forge.hold.set()
    await read

    # The read was covered by the invalidation of MR 1, so it was not stored,
    # and with nothing in flight every mark is gone.
    assert await _cached_keys(store) == set()
    assert len(client._marked_generations) == 0
    assert client._read_generations == {}


@pytest.mark.asyncio
async def test_a_cancelled_read_releases_its_generation(store: CacheStore) -> None:
    forge = _Forge()
    client = _client(forge, store)
    forge.hold = asyncio.Event()
    read = asyncio.create_task(client.list_mrs("acme/repo"))
    await forge.started.wait()
    await client.invalidate_repository_reads("acme/repo")
    assert len(client._marked_generations) == 1

    read.cancel()
    with pytest.raises(asyncio.CancelledError):
        await read

    assert client._read_generations == {}
    assert len(client._marked_generations) == 0
