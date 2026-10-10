"""Tests for CacheStore."""

from unittest.mock import patch

import pytest
import pytest_asyncio

from tongs.cache.store import CacheStore


@pytest_asyncio.fixture
async def store(tmp_path):
    s = CacheStore(db_path=tmp_path / "test.db", max_size_mb=1)
    await s.open()
    yield s
    await s.close()


class TestPutGet:
    @pytest.mark.asyncio
    async def test_roundtrip(self, store):
        """put then get returns the same bytes."""
        await store.put("key1", b"hello", ttl=60)
        result = await store.get("key1")
        assert result == b"hello"

    @pytest.mark.asyncio
    async def test_ttl_expiration(self, store):
        """Expired entries are not returned by get."""
        await store.put("expire_me", b"data", ttl=10)
        # Advance time past expiration
        with patch("tongs.cache.store.time") as mock_time:
            mock_time.time.return_value = 9999999999.0
            result = await store.get("expire_me")
        assert result is None


class TestExcludedPrefixes:
    @pytest.mark.asyncio
    async def test_put_excluded_job_log_is_noop(self, store):
        """Keys starting with job_log: are silently ignored on put."""
        await store.put("job_log:abc123", b"log data", ttl=60)
        # Bypass exclusion check by querying DB directly
        cursor = await store._db.execute(
            "SELECT value FROM cache WHERE key = ?", ("job_log:abc123",)
        )
        row = await cursor.fetchone()
        assert row is None

    @pytest.mark.asyncio
    async def test_get_excluded_stream_log_is_noop(self, store):
        """Keys starting with stream_log: always return None on get."""
        # Force-insert via SQL to bypass put exclusion
        import time

        now = time.time()
        await store._db.execute(
            "INSERT INTO cache (key, value, expires_at, created_at, size_bytes) "
            "VALUES (?, ?, ?, ?, ?)",
            ("stream_log:xyz", b"data", now + 3600, now, 4),
        )
        await store._db.commit()
        result = await store.get("stream_log:xyz")
        assert result is None


class TestInvalidate:
    @pytest.mark.asyncio
    async def test_invalidate_removes_key(self, store):
        """invalidate removes a single key."""
        await store.put("remove_me", b"data", ttl=60)
        await store.invalidate("remove_me")
        assert await store.get("remove_me") is None

    @pytest.mark.asyncio
    async def test_invalidate_prefix_removes_matching_only(self, store):
        """invalidate_prefix removes keys with the prefix, leaves others."""
        await store.put("pr:1", b"a", ttl=60)
        await store.put("pr:2", b"b", ttl=60)
        await store.put("ci:1", b"c", ttl=60)
        await store.invalidate_prefix("pr:")
        assert await store.get("pr:1") is None
        assert await store.get("pr:2") is None
        assert await store.get("ci:1") == b"c"

    @pytest.mark.asyncio
    async def test_invalidate_prefix_stops_at_a_key_boundary(self, store):
        """MR 1 does not cover MR 12, and a repository not its sibling."""
        keys = (
            "github.com:o/r",
            "github.com:o/r:mr:1",
            "github.com:o/r:mr:1:diff",
            "github.com:o/r:mr:12:diff",
            "github.com:o/r-docs:mrs:open",
        )
        for key in keys:
            await store.put(key, b"v", ttl=60)

        await store.invalidate_prefix("github.com:o/r:mr:1")
        remaining = await _keys(store)
        assert remaining == {
            "github.com:o/r",
            "github.com:o/r:mr:12:diff",
            "github.com:o/r-docs:mrs:open",
        }

        await store.invalidate_prefix("github.com:o/r")
        assert await _keys(store) == {"github.com:o/r-docs:mrs:open"}

    @pytest.mark.asyncio
    async def test_invalidate_prefix_matches_wildcards_literally(self, store):
        """'_' and '%' in a path match only themselves."""
        await store.put("github.com:o/a_b:mrs:open", b"1", ttl=60)
        await store.put("github.com:o/axb:mrs:open", b"2", ttl=60)
        await store.put("github.com:o/a%b:mrs:open", b"3", ttl=60)
        await store.put("github.com:o/a-long-b:mrs:open", b"4", ttl=60)

        await store.invalidate_prefix("github.com:o/a_b")
        await store.invalidate_prefix("github.com:o/a%b")

        assert await _keys(store) == {
            "github.com:o/axb:mrs:open",
            "github.com:o/a-long-b:mrs:open",
        }

    @pytest.mark.asyncio
    async def test_invalidate_prefix_ignores_case(self, store):
        """Forge paths ignore case, so the invalidation does as well."""
        await store.put("github.com:Acme/Repo:mrs:open", b"1", ttl=60)
        await store.put("github.com:acme/repository:mrs:open", b"2", ttl=60)

        await store.invalidate_prefix("github.com:acme/repo:")

        assert await _keys(store) == {"github.com:acme/repository:mrs:open"}


class TestClearAndPrune:
    @pytest.mark.asyncio
    async def test_clear_removes_all(self, store):
        """clear empties the entire cache."""
        await store.put("a", b"1", ttl=60)
        await store.put("b", b"2", ttl=60)
        await store.clear()
        assert await store.get("a") is None
        assert await store.get("b") is None

    @pytest.mark.asyncio
    async def test_prune_removes_only_expired(self, store):
        """prune deletes expired entries but keeps live ones."""
        import time

        now = time.time()
        # Insert one expired and one live entry via SQL for precise control
        await store._db.execute(
            "INSERT INTO cache (key, value, expires_at, created_at, size_bytes) "
            "VALUES (?, ?, ?, ?, ?)",
            ("expired", b"old", now - 1, now - 100, 3),
        )
        await store._db.execute(
            "INSERT INTO cache (key, value, expires_at, created_at, size_bytes) "
            "VALUES (?, ?, ?, ?, ?)",
            ("live", b"fresh", now + 3600, now, 5),
        )
        await store._db.commit()
        await store.prune()
        assert await store.get("expired") is None
        assert await store.get("live") == b"fresh"


class TestSizeLimit:
    @pytest.mark.asyncio
    async def test_oldest_quarter_by_recency_is_evicted_past_the_cap(self, store):
        """Past the size cap the least recently used quarter goes, newest stay."""
        clock = iter(range(1, 100))
        chunk = b"x" * (128 * 1024)
        with patch("tongs.cache.store.time") as mock_time:
            mock_time.time.side_effect = lambda: 1_000_000.0 + next(clock)
            for index in range(8):
                await store.put(f"k{index}", chunk, ttl=3600)
            assert await store.get("k0") == chunk
            await store.put("k8", chunk, ttl=3600)

        cursor = await store._db.execute("SELECT key FROM cache")
        remaining = {row[0] for row in await cursor.fetchall()}
        assert remaining == {"k0", "k3", "k4", "k5", "k6", "k7", "k8"}

    @pytest.mark.asyncio
    async def test_a_single_oversized_entry_is_evicted(self, store):
        """Below four rows a quarter is zero rows; one row still has to go."""
        await store.put("huge", b"x" * (1024 * 1024 + 1), ttl=3600)

        assert await _keys(store) == set()

    @pytest.mark.asyncio
    async def test_eviction_repeats_until_the_total_fits(self, store):
        """One quarter is not always enough; eviction ends under the limit."""
        clock = iter(range(1, 100))
        small = b"x" * 1024
        with patch("tongs.cache.store.time") as mock_time:
            mock_time.time.side_effect = lambda: 1_000_000.0 + next(clock)
            for index in range(7):
                await store.put(f"small{index}", small, ttl=3600)
            await store.put("big", b"y" * (1024 * 1024 - 2 * 1024), ttl=3600)

        cursor = await store._db.execute(
            "SELECT COALESCE(SUM(size_bytes), 0) FROM cache"
        )
        total = (await cursor.fetchone())[0]
        assert 0 < total <= 1024 * 1024
        # A single quarter (two rows) leaves the file 3 KiB over; further
        # passes drop the least recently used rows until it fits, and the
        # newest entry stays.
        assert await _keys(store) == {"small5", "small6", "big"}


class TestNotOpened:
    @pytest.mark.asyncio
    async def test_operations_noop_when_not_opened(self, tmp_path):
        """All operations are silent noops when open() was never called."""
        s = CacheStore(db_path=tmp_path / "unopened.db", max_size_mb=1)
        # None of these should raise
        await s.put("k", b"v", ttl=60)
        assert await s.get("k") is None
        assert await s.get_json("k") is None
        await s.invalidate("k")
        await s.invalidate_prefix("k")
        await s.clear()
        await s.prune()
        await s.close()


class TestCloseIsTerminal:
    @pytest.mark.asyncio
    async def test_operations_noop_after_close(self, tmp_path):
        """After close(), operations silently noop."""
        s = CacheStore(db_path=tmp_path / "closed.db", max_size_mb=1)
        await s.open()
        await s.put("k", b"v", ttl=60)
        await s.close()
        # All operations should noop without raising
        assert await s.get("k") is None
        await s.put("k2", b"v2", ttl=60)
        assert await s.get("k2") is None
        await s.invalidate("k")
        await s.invalidate_prefix("k")
        await s.clear()
        await s.prune()


async def _keys(store: CacheStore) -> set[str]:
    cursor = await store._db.execute("SELECT key FROM cache")
    return {row[0] for row in await cursor.fetchall()}
