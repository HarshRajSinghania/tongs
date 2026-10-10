"""Paged repository review reads in the application session (#344)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

import tongs.services.session as session_module
from tests.services.test_session import (
    NOW,
    FakeClient,
    FakeRegistry,
    make_summary,
    start_session,
)
from tongs.errors import NetworkError, PartialResultsError, RateLimitError
from tongs.forges.models import CIStatus, MRPage, MRState
from tongs.services import (
    RepositoryRef,
    ReviewQuery,
    ReviewRef,
    ReviewScope,
    ServiceError,
    ServiceErrorCode,
)

_REPOSITORY = RepositoryRef("github.com", "acme/widgets")


class _PagedClient(FakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.repository_reviews = [
            make_summary(number=n, updated_at=NOW - timedelta(minutes=n))
            for n in range(1, 6)
        ]
        self.page_calls: list[tuple[str, str, int, int]] = []

    async def list_mrs_page(
        self, repo_path: str, state: str = "open", page: int = 1, per_page: int = 100
    ) -> MRPage:
        self.page_calls.append((repo_path, state, page, per_page))
        start = (page - 1) * per_page
        items = tuple(self.repository_reviews[start : start + per_page])
        return MRPage(items, has_next=start + per_page < len(self.repository_reviews))


async def _session_with(client: FakeClient):
    session = await start_session(FakeRegistry({"github.com": client}))
    await session.open_repository("github.com", "acme/widgets")
    return session


@pytest.mark.asyncio
async def test_paged_repository_read_returns_next_cursor_until_last_page() -> None:
    client = _PagedClient()
    session = await _session_with(client)
    client.page_calls.clear()

    numbers: list[int] = []
    cursors: list[str | None] = []
    cursor: str | None = None
    while True:
        page = await session.list_reviews(
            ReviewQuery(
                ReviewScope.ALL_OPEN,
                repository=_REPOSITORY,
                per_page=2,
                paged=True,
                cursor=cursor,
            )
        )
        numbers.extend(item.summary.number for item in page.items)
        cursors.append(page.next_cursor)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor

    assert numbers == [1, 2, 3, 4, 5]
    assert cursors == ["2", "3", None]
    assert client.page_calls == [
        ("acme/widgets", "open", 1, 2),
        ("acme/widgets", "open", 2, 2),
        ("acme/widgets", "open", 3, 2),
    ]
    assert client.list_mrs_calls == 0
    await session.close()


@pytest.mark.asyncio
async def test_unpaged_query_keeps_walking_every_review() -> None:
    client = _PagedClient()
    session = await _session_with(client)
    client.page_calls.clear()

    page = await session.list_reviews(
        ReviewQuery(ReviewScope.ALL_OPEN, repository=_REPOSITORY, per_page=2)
    )

    assert [item.summary.number for item in page.items] == [1, 2, 3, 4, 5]
    assert page.next_cursor is None
    assert client.page_calls == []
    assert client.list_mrs_calls == 1
    await session.close()


@pytest.mark.asyncio
async def test_open_repository_reads_one_single_item_page() -> None:
    client = _PagedClient()
    session = await _session_with(client)
    assert client.page_calls == [("acme/widgets", "open", 1, 1)]
    assert client.list_mrs_calls == 0
    await session.close()


@pytest.mark.asyncio
async def test_invalid_page_from_forge_is_an_invalid_response() -> None:
    class _BrokenClient(_PagedClient):
        async def list_mrs_page(self, *_args: object, **_kwargs: object) -> MRPage:
            return cast(MRPage, [make_summary()])

    client = _BrokenClient()
    session = await start_session(FakeRegistry({"github.com": client}))
    with pytest.raises(ServiceError) as caught:
        await session.open_repository("github.com", "acme/widgets")
    assert caught.value.code is ServiceErrorCode.INVALID_RESPONSE
    await session.close()


@pytest.mark.parametrize(
    "cursor", ["1", "0", "02", "abc", "", "100000", " 2", "٢", "2.5"]
)
def test_malformed_cursors_are_rejected(cursor: str) -> None:
    with pytest.raises(ValueError):
        ReviewQuery(
            ReviewScope.ALL_OPEN, repository=_REPOSITORY, paged=True, cursor=cursor
        )


@pytest.mark.parametrize(
    "query",
    [
        {"scope": ReviewScope.ALL_OPEN, "repository": _REPOSITORY},
        {"scope": ReviewScope.ALL_OPEN, "paged": True},
        {"scope": ReviewScope.MY_REVIEWS, "repository": _REPOSITORY, "paged": True},
    ],
)
def test_cursor_needs_a_paged_all_open_repository_read(query: dict) -> None:
    with pytest.raises(ValueError):
        ReviewQuery(**query, cursor="2")


def test_cursor_query_accepts_a_later_page() -> None:
    query = ReviewQuery(
        ReviewScope.ALL_OPEN,
        repository=_REPOSITORY,
        state=MRState.CLOSED,
        paged=True,
        cursor="35",
    )
    assert query.cursor == "35"


class _FlakyPagedClient(_PagedClient):
    def __init__(self) -> None:
        super().__init__()
        self.failing_pages: set[int] = set()

    async def list_mrs_page(
        self, repo_path: str, state: str = "open", page: int = 1, per_page: int = 100
    ) -> MRPage:
        if page in self.failing_pages:
            self.page_calls.append((repo_path, state, page, per_page))
            raise TimeoutError("controlled page timeout")
        return await super().list_mrs_page(repo_path, state, page, per_page)


@pytest.mark.asyncio
async def test_failed_later_page_keeps_its_cursor_for_a_retry() -> None:
    client = _FlakyPagedClient()
    session = await _session_with(client)
    client.failing_pages.add(2)
    query = {
        "scope": ReviewScope.ALL_OPEN,
        "repository": _REPOSITORY,
        "per_page": 2,
        "paged": True,
    }

    failed = await session.list_reviews(ReviewQuery(**query, cursor="2"))

    assert failed.items == ()
    assert len(failed.failures) == 1
    assert failed.failures[0].repository == _REPOSITORY
    assert failed.next_cursor == "2"

    client.failing_pages.clear()
    retried = await session.list_reviews(
        ReviewQuery(**query, cursor=failed.next_cursor)
    )
    assert [item.summary.number for item in retried.items] == [3, 4]
    assert retried.failures == ()
    assert retried.next_cursor == "3"
    await session.close()


@pytest.mark.asyncio
async def test_failed_first_page_has_no_cursor() -> None:
    client = _FlakyPagedClient()
    session = await _session_with(client)
    client.failing_pages.add(1)

    page = await session.list_reviews(
        ReviewQuery(
            ReviewScope.ALL_OPEN, repository=_REPOSITORY, per_page=2, paged=True
        )
    )

    assert page.items == ()
    assert len(page.failures) == 1
    assert page.next_cursor is None
    await session.close()


class _ForeignPageClient(_PagedClient):
    """Returns a summary of another project on the chosen pages."""

    def __init__(self) -> None:
        super().__init__()
        self.foreign_pages: set[int] = set()

    async def list_mrs_page(
        self, repo_path: str, state: str = "open", page: int = 1, per_page: int = 100
    ) -> MRPage:
        result = await super().list_mrs_page(repo_path, state, page, per_page)
        if page not in self.foreign_pages:
            return result
        foreign = make_summary(project="other/project", number=99)
        return MRPage((foreign, *result.items[1:]), has_next=result.has_next)


@pytest.mark.asyncio
async def test_first_page_that_fails_validation_has_no_cursor() -> None:
    client = _ForeignPageClient()
    session = await _session_with(client)
    client.foreign_pages.add(1)

    page = await session.list_reviews(
        ReviewQuery(
            ReviewScope.ALL_OPEN, repository=_REPOSITORY, per_page=2, paged=True
        )
    )

    assert page.items == ()
    assert [failure.code for failure in page.failures] == [
        ServiceErrorCode.INVALID_RESPONSE
    ]
    # The forge said a page 2 exists, but page 1 was never shown, so the
    # caller must read page 1 again instead of skipping past it.
    assert page.next_cursor is None
    await session.close()


@pytest.mark.asyncio
async def test_later_page_that_fails_validation_keeps_its_cursor() -> None:
    client = _ForeignPageClient()
    session = await _session_with(client)
    client.foreign_pages.add(2)

    page = await session.list_reviews(
        ReviewQuery(
            ReviewScope.ALL_OPEN,
            repository=_REPOSITORY,
            per_page=2,
            paged=True,
            cursor="2",
        )
    )

    assert page.items == ()
    assert len(page.failures) == 1
    assert page.next_cursor == "2"
    await session.close()


class _LazyClient(_PagedClient):
    """Pages without CI on request and looks CI up per review (#396)."""

    supports_lazy_ci = True

    def __init__(self) -> None:
        super().__init__()
        self.with_ci: list[bool] = []
        self.lookups: list[tuple[str, str, int, str]] = []
        self.lookup_error: Exception | None = None
        self.lookup_result: object = CIStatus.FAILED

    async def list_mrs_page(
        self,
        repo_path: str,
        state: str = "open",
        page: int = 1,
        per_page: int = 100,
        *,
        with_ci: bool = True,
    ) -> MRPage:
        self.with_ci.append(with_ci)
        return await super().list_mrs_page(repo_path, state, page, per_page)

    async def get_mr_ci_status(
        self, repo_path: str, number: int, head_sha: str = ""
    ) -> CIStatus:
        return await self._lookup("cached", repo_path, number, head_sha)

    async def get_mr_ci_status_fresh(
        self, repo_path: str, number: int, head_sha: str = ""
    ) -> CIStatus:
        return await self._lookup("fresh", repo_path, number, head_sha)

    async def _lookup(
        self, kind: str, repo_path: str, number: int, head_sha: str
    ) -> CIStatus:
        self.lookups.append((kind, repo_path, number, head_sha))
        if self.lookup_error is not None:
            raise self.lookup_error
        return cast(CIStatus, self.lookup_result)


@pytest.mark.asyncio
async def test_a_paged_read_without_ci_reaches_a_lazy_client() -> None:
    client = _LazyClient()
    session = await _session_with(client)
    client.with_ci.clear()

    await session.list_reviews(
        ReviewQuery(
            ReviewScope.ALL_OPEN, repository=_REPOSITORY, paged=True, with_ci=False
        )
    )
    await session.list_reviews(
        ReviewQuery(ReviewScope.ALL_OPEN, repository=_REPOSITORY, paged=True)
    )

    # The desktop's paged read keeps CI on the list; only an opt-out skips it.
    assert client.with_ci == [False, True]
    await session.close()


@pytest.mark.asyncio
async def test_a_client_without_lazy_ci_keeps_its_page_with_ci() -> None:
    client = _PagedClient()
    session = await _session_with(client)

    page = await session.list_reviews(
        ReviewQuery(
            ReviewScope.ALL_OPEN, repository=_REPOSITORY, paged=True, with_ci=False
        )
    )

    assert {item.summary.ci_status for item in page.items} == {CIStatus.SUCCESS}
    await session.close()


def test_with_ci_must_be_a_boolean() -> None:
    with pytest.raises(TypeError):
        ReviewQuery(ReviewScope.ALL_OPEN, with_ci=cast(bool, 0))


@pytest.mark.asyncio
async def test_ci_lookup_names_the_review_and_its_head() -> None:
    client = _LazyClient()
    session = await _session_with(client)
    review = ReviewRef(_REPOSITORY, 7)

    cached = await session.get_review_ci_status(review, head_sha="sha-7")
    fresh = await session.get_review_ci_status(review, head_sha="sha-7", fresh=True)

    assert (cached, fresh) == (CIStatus.FAILED, CIStatus.FAILED)
    assert client.lookups == [
        ("cached", "acme/widgets", 7, "sha-7"),
        ("fresh", "acme/widgets", 7, "sha-7"),
    ]
    await session.close()


@pytest.mark.asyncio
async def test_ci_lookup_needs_an_issued_repository() -> None:
    client = _LazyClient()
    session = await _session_with(client)

    with pytest.raises(ServiceError) as caught:
        await session.get_review_ci_status(
            ReviewRef(RepositoryRef("github.com", "acme/other"), 7)
        )

    assert caught.value.code is ServiceErrorCode.RESOURCE_NOT_ISSUED
    assert client.lookups == []
    await session.close()


@pytest.mark.asyncio
async def test_ci_lookup_failures_are_translated() -> None:
    client = _LazyClient()
    client.lookup_error = NetworkError("private transport detail")
    session = await _session_with(client)

    with pytest.raises(ServiceError) as caught:
        await session.get_review_ci_status(ReviewRef(_REPOSITORY, 7))

    assert caught.value.code is ServiceErrorCode.NETWORK_UNAVAILABLE
    assert "private" not in caught.value.message
    await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "result"),
    [
        ({"head_sha": cast(str, None)}, CIStatus.SUCCESS),
        ({"head_sha": "x" * 129}, CIStatus.SUCCESS),
        ({"head_sha": "../../x"}, CIStatus.SUCCESS),
        ({}, "success"),
    ],
)
async def test_ci_lookup_rejects_bad_input_and_bad_results(
    kwargs: dict[str, object], result: object
) -> None:
    client = _LazyClient()
    client.lookup_result = result
    session = await _session_with(client)

    with pytest.raises(ServiceError) as caught:
        await session.get_review_ci_status(ReviewRef(_REPOSITORY, 7), **kwargs)  # type: ignore[arg-type]

    expected = (
        ServiceErrorCode.INVALID_RESPONSE
        if not kwargs
        else ServiceErrorCode.INVALID_INPUT
    )
    assert caught.value.code is expected
    await session.close()


@pytest.mark.asyncio
async def test_a_client_without_a_ci_lookup_reports_unknown() -> None:
    client = _PagedClient()
    session = await _session_with(client)

    status = await session.get_review_ci_status(ReviewRef(_REPOSITORY, 7))

    assert status is CIStatus.UNKNOWN
    await session.close()


@pytest.mark.asyncio
async def test_a_ci_rate_limit_is_kept_with_its_reset_until_it_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 10, 10, 18, 0, tzinfo=UTC)
    monkeypatch.setattr(session_module, "_utcnow", lambda: now)
    client = _LazyClient()
    client.lookup_error = RateLimitError("Rate limited", retry_after=1800)
    session = await _session_with(client)
    assert session.host_rate_limit("github.com") is None

    with pytest.raises(ServiceError) as caught:
        await session.get_review_ci_status(ReviewRef(_REPOSITORY, 7))

    assert caught.value.code is ServiceErrorCode.RATE_LIMITED
    limit = session.host_rate_limit("github.com")
    assert limit is not None
    assert limit.resets_at == limit.until == now + timedelta(minutes=30)
    assert session.host_rate_limit("gitlab.com") is None

    # Once the reset passes the limit is gone.
    monkeypatch.setattr(
        session_module, "_utcnow", lambda: now + timedelta(minutes=30, seconds=1)
    )
    assert session.host_rate_limit("github.com") is None
    await session.close()


@pytest.mark.asyncio
async def test_a_list_rate_limit_without_a_reset_waits_a_few_minutes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 10, 10, 18, 0, tzinfo=UTC)
    monkeypatch.setattr(session_module, "_utcnow", lambda: now)

    class _LimitedClient(_PagedClient):
        limited = False

        async def list_mrs_page(self, *args: object, **kwargs: object) -> MRPage:
            if self.limited:
                raise RateLimitError("Rate limited")
            return await super().list_mrs_page(*args, **kwargs)  # type: ignore[arg-type]

    client = _LimitedClient()
    session = await _session_with(client)
    client.limited = True
    page = await session.list_reviews(
        ReviewQuery(ReviewScope.ALL_OPEN, repository=_REPOSITORY, paged=True)
    )

    assert [failure.code for failure in page.failures] == [
        ServiceErrorCode.RATE_LIMITED
    ]
    limit = session.host_rate_limit("github.com")
    assert limit is not None
    assert limit.resets_at is None
    assert limit.until == now + session_module.RATE_LIMIT_UNKNOWN_WAIT
    await session.close()


@pytest.mark.asyncio
async def test_a_search_cut_short_by_a_rate_limit_records_it() -> None:
    class _CutShort(_PagedClient):
        async def list_my_reviews(self) -> list:
            raise PartialResultsError(
                [], cause=RateLimitError("Rate limited", retry_after=60)
            )

    session = await _session_with(_CutShort())
    page = await session.list_reviews(ReviewQuery(ReviewScope.MY_REVIEWS))

    assert page.failures[0].code is ServiceErrorCode.RATE_LIMITED
    limit = session.host_rate_limit("github.com")
    assert limit is not None and limit.resets_at is not None
    await session.close()
