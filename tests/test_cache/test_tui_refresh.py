"""Ctrl+R in the terminal inbox reads past the review list cache (#309)."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from tests.test_tui_session import (
    MockForgeClient,
    MockForgeRegistry,
    make_app,
    make_repo,
    make_summary,
    settle,
)
from tongs.cache.cached_client import CachedForgeClient
from tongs.cache.store import CacheStore
from tongs.forges.base import ForgeClient
from tongs.forges.models import ForgeHost
from tongs.scanner.repo import ForgeType
from tongs.services.models import ReviewPage, ReviewQuery, ReviewScope
from tongs.widgets.mr_table import MRTable


@pytest.mark.asyncio
async def test_ctrl_r_on_all_open_reads_the_forge_past_the_cache(
    tmp_path: Path,
) -> None:
    host = ForgeHost("github.com", ForgeType.GITHUB, "https://api.github.com")
    forge = MockForgeClient(host, [make_summary(host)])
    cache = CacheStore(tmp_path / "cache" / "cache.db")
    client = CachedForgeClient(cast(ForgeClient, forge), cache, hostname="github.com")
    registry = MockForgeRegistry({host.hostname: cast(MockForgeClient, client)})
    app, session, _cache = make_app(
        tmp_path, [make_repo(tmp_path)], registry, cache=cache
    )
    queries: list[ReviewQuery] = []
    list_reviews = session.list_reviews

    async def recording_list_reviews(query: ReviewQuery) -> ReviewPage:
        queries.append(query)
        return await list_reviews(query)

    session.list_reviews = recording_list_reviews  # type: ignore[method-assign]

    async with app.run_test() as pilot:
        await settle(app)
        await pilot.press("3")
        await settle(app)
        table = app.screen.query_one("#all-open-table", MRTable)
        assert table.row_count == 1
        assert forge.calls.count(("all_open", "acme/widgets")) == 1

        # A review opened on the forge; a plain read still gets the cached list.
        forge.summaries.append(make_summary(host, number=8))
        cached = await app.services.list_reviews(ReviewScope.ALL_OPEN)
        assert [item.ref.number for item in cached.items] == [7]
        assert forge.calls.count(("all_open", "acme/widgets")) == 1

        await pilot.press("ctrl+r")
        await settle(app)
        assert forge.calls.count(("all_open", "acme/widgets")) == 2
        assert table.row_count == 2

    all_open = [query for query in queries if query.scope is ReviewScope.ALL_OPEN]
    assert [query.fresh for query in all_open] == [False, False, True]


@pytest.mark.asyncio
async def test_ctrl_r_on_a_personal_tab_sends_a_fresh_query(tmp_path: Path) -> None:
    host = ForgeHost("github.com", ForgeType.GITHUB, "https://api.github.com")
    forge = MockForgeClient(host, [make_summary(host)])
    registry = MockForgeRegistry({host.hostname: forge})
    app, session, _cache = make_app(tmp_path, [make_repo(tmp_path)], registry)
    queries: list[ReviewQuery] = []
    list_reviews = session.list_reviews

    async def recording_list_reviews(query: ReviewQuery) -> ReviewPage:
        queries.append(query)
        return await list_reviews(query)

    session.list_reviews = recording_list_reviews  # type: ignore[method-assign]

    async with app.run_test() as pilot:
        await settle(app)
        await pilot.press("ctrl+r")
        await settle(app)

    assert [(query.scope, query.fresh) for query in queries] == [
        (ReviewScope.MY_REVIEWS, False),
        (ReviewScope.MY_REVIEWS, True),
    ]
