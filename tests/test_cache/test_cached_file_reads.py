"""Persistent caching of file reads at a commit (#381)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio

from tongs.cache.cached_client import (
    FILE_CACHE_MAX_BYTES,
    FILE_CONTENT_TTL,
    CachedForgeClient,
)
from tongs.cache.store import CacheStore
from tongs.errors import (
    ContentTooLargeError,
    NotFoundError,
    UnsupportedFileReadError,
)
from tongs.forges.registry import ForgeRegistry

SHA = "0123456789abcdef0123456789abcdef01234567"
SHA256 = "ab" * 32


class FileClient:
    """A duck-typed forge client that serves files and counts reads."""

    def __init__(self, files: dict[tuple[str, str], bytes] | None = None) -> None:
        self.files = files or {}
        self.calls: list[tuple[str, str, str, int]] = []
        self.error: Exception | None = None

    async def get_file_at_ref(
        self, repo_path: str, path: str, ref: str, *, max_bytes: int
    ) -> bytes:
        self.calls.append((repo_path, path, ref, max_bytes))
        if self.error is not None:
            raise self.error
        try:
            body = self.files[(path, ref)]
        except KeyError:
            raise NotFoundError("Not found") from None
        if len(body) > max_bytes:
            raise ContentTooLargeError(max_bytes)
        return body

    async def close(self) -> None:
        return None


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[CacheStore]:
    cache = CacheStore(db_path=tmp_path / "cache.db", max_size_mb=10)
    await cache.open()
    yield cache
    await cache.close()


def wrap(inner: object, store: CacheStore, **kwargs: int) -> CachedForgeClient:
    return CachedForgeClient(inner, store, "github.com", **kwargs)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_second_read_is_served_from_the_database(store: CacheStore) -> None:
    inner = FileClient({("src/a.py", SHA): b"print(1)\n"})
    first = wrap(inner, store)
    assert await first.get_file_at_ref("acme/w", "src/a.py", SHA, max_bytes=100) == (
        b"print(1)\n"
    )
    # A new wrapper over the same database, as in a later session.
    second = wrap(inner, store)
    assert await second.get_file_at_ref("acme/w", "src/a.py", SHA, max_bytes=100) == (
        b"print(1)\n"
    )
    assert len(inner.calls) == 1


@pytest.mark.asyncio
async def test_sha256_commits_are_cached(store: CacheStore) -> None:
    inner = FileClient({("a.py", SHA256): b"x"})
    client = wrap(inner, store)
    for _ in range(2):
        assert await client.get_file_at_ref("acme/w", "a.py", SHA256, max_bytes=10)
    assert len(inner.calls) == 1


@pytest.mark.asyncio
async def test_body_over_one_mebibyte_is_not_persisted(store: CacheStore) -> None:
    body = b"x" * (FILE_CACHE_MAX_BYTES + 1)
    inner = FileClient({("big.txt", SHA): body})
    client = wrap(inner, store)
    for _ in range(2):
        assert (
            await client.get_file_at_ref(
                "acme/w", "big.txt", SHA, max_bytes=2 * FILE_CACHE_MAX_BYTES
            )
            == body
        )
    assert len(inner.calls) == 2


@pytest.mark.asyncio
async def test_body_of_exactly_one_mebibyte_is_persisted(store: CacheStore) -> None:
    body = b"x" * FILE_CACHE_MAX_BYTES
    inner = FileClient({("edge.txt", SHA): body})
    client = wrap(inner, store)
    for _ in range(2):
        await client.get_file_at_ref(
            "acme/w", "edge.txt", SHA, max_bytes=FILE_CACHE_MAX_BYTES
        )
    assert len(inner.calls) == 1


@pytest.mark.asyncio
async def test_paths_differing_only_in_case_get_separate_entries(
    store: CacheStore,
) -> None:
    inner = FileClient({("README.md", SHA): b"upper", ("readme.md", SHA): b"lower"})
    client = wrap(inner, store)
    assert await client.get_file_at_ref("acme/w", "README.md", SHA, max_bytes=10) == (
        b"upper"
    )
    assert await client.get_file_at_ref("acme/w", "readme.md", SHA, max_bytes=10) == (
        b"lower"
    )
    assert await client.get_file_at_ref("acme/w", "README.md", SHA, max_bytes=10) == (
        b"upper"
    )
    assert len(inner.calls) == 2


@pytest.mark.asyncio
async def test_repositories_and_commits_get_separate_entries(
    store: CacheStore,
) -> None:
    other = "f" * 40
    inner = FileClient({("a.py", SHA): b"one", ("a.py", other): b"two"})
    client = wrap(inner, store)
    assert await client.get_file_at_ref("acme/w", "a.py", SHA, max_bytes=10) == b"one"
    assert await client.get_file_at_ref("acme/w", "a.py", other, max_bytes=10) == (
        b"two"
    )
    assert await client.get_file_at_ref("acme/x", "a.py", SHA, max_bytes=10) == b"one"
    assert len(inner.calls) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("ref", ["main", "0123abc", SHA.upper()])
async def test_refs_other_than_full_shas_are_never_cached(
    store: CacheStore, ref: str
) -> None:
    inner = FileClient({("a.py", ref): b"x"})
    client = wrap(inner, store)
    for _ in range(2):
        await client.get_file_at_ref("acme/w", "a.py", ref, max_bytes=10)
    assert len(inner.calls) == 2


@pytest.mark.asyncio
async def test_cached_body_longer_than_the_limit_is_a_miss(store: CacheStore) -> None:
    inner = FileClient({("a.py", SHA): b"x" * 50})
    client = wrap(inner, store)
    await client.get_file_at_ref("acme/w", "a.py", SHA, max_bytes=100)
    with pytest.raises(ContentTooLargeError):
        await client.get_file_at_ref("acme/w", "a.py", SHA, max_bytes=10)
    assert [call[3] for call in inner.calls] == [100, 10]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        NotFoundError("Not found"),
        ContentTooLargeError(10),
        UnsupportedFileReadError(),
    ],
)
async def test_errors_are_never_cached(store: CacheStore, error: Exception) -> None:
    inner = FileClient()
    inner.error = error
    client = wrap(inner, store)
    for _ in range(2):
        with pytest.raises(type(error)):
            await client.get_file_at_ref("acme/w", "a.py", SHA, max_bytes=10)
    assert len(inner.calls) == 2


@pytest.mark.asyncio
async def test_clearing_the_cache_removes_file_entries(store: CacheStore) -> None:
    inner = FileClient({("a.py", SHA): b"x"})
    client = wrap(inner, store)
    await client.get_file_at_ref("acme/w", "a.py", SHA, max_bytes=10)
    await store.clear()
    await client.get_file_at_ref("acme/w", "a.py", SHA, max_bytes=10)
    assert len(inner.calls) == 2


@pytest.mark.asyncio
async def test_review_writes_leave_file_entries_alone(store: CacheStore) -> None:
    inner = FileClient({("a.py", SHA): b"x"})
    client = wrap(inner, store)
    await client.get_file_at_ref("acme/w", "a.py", SHA, max_bytes=10)
    assert await client.invalidate_review_reads("acme/w", 7)
    await client.get_file_at_ref("acme/w", "a.py", SHA, max_bytes=10)
    assert len(inner.calls) == 1


@pytest.mark.asyncio
async def test_default_ttl_is_one_day(store: CacheStore) -> None:
    assert FILE_CONTENT_TTL == 86_400
    ttls: list[int] = []
    original = store.put

    async def recording_put(key: str, value: bytes, ttl: int) -> None:
        ttls.append(ttl)
        await original(key, value, ttl)

    inner = FileClient({("a.py", SHA): b"x"})
    with patch.object(store, "put", recording_put):
        await wrap(inner, store).get_file_at_ref("acme/w", "a.py", SHA, max_bytes=10)
    assert ttls == [86_400]


@pytest.mark.asyncio
async def test_configured_ttl_reaches_the_cache_put(store: CacheStore) -> None:
    ttls: list[int] = []
    original = store.put

    async def recording_put(key: str, value: bytes, ttl: int) -> None:
        ttls.append(ttl)
        await original(key, value, ttl)

    inner = FileClient({("a.py", SHA): b"x"})
    with patch.object(store, "put", recording_put):
        client = wrap(inner, store, file_content_ttl=7_200)
        await client.get_file_at_ref("acme/w", "a.py", SHA, max_bytes=10)
    assert ttls == [7_200]


@pytest.mark.asyncio
async def test_registry_passes_the_ttl_to_the_cache_wrapper(
    store: CacheStore,
) -> None:
    registry = ForgeRegistry(cache=store, file_content_ttl=3_600)
    with patch("tongs.forges.registry.resolve_token", return_value="tok"):
        client = await registry.get_client("github.com")
    try:
        assert isinstance(client, CachedForgeClient)
        assert client._file_content_ttl == 3_600
    finally:
        await registry.close_all()


@pytest.mark.asyncio
async def test_inner_client_without_file_reads_is_unsupported(
    store: CacheStore,
) -> None:
    client = wrap(object(), store)
    with pytest.raises(UnsupportedFileReadError):
        await client.get_file_at_ref("acme/w", "a.py", SHA, max_bytes=10)
