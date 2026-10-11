"""The GitHub merge base, its cache, and same-API redirects on bounded reads (#382)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio

from tongs.cache.cached_client import MERGE_BASE_TTL, CachedForgeClient
from tongs.cache.store import CacheStore
from tongs.errors import (
    ContentTooLargeError,
    NotFoundError,
    UnsupportedFileReadError,
    ValidationError,
)
from tongs.forges.base import (
    is_full_commit_sha,
    read_merge_base,
    revision_base_is_merge_base,
)
from tongs.forges.github import MERGE_BASE_MAX_BYTES, GitHubClient
from tongs.forges.gitlab import GitLabClient
from tongs.forges.http import MAX_READ_REDIRECTS, HostRequestLimiter, request_bytes
from tongs.forges.models import ForgeHost
from tongs.scanner.repo import ForgeType

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "forge_files"
GITHUB_HOST = ForgeHost("github.com", ForgeType.GITHUB, "https://api.github.com")
GITLAB_HOST = ForgeHost("gitlab.com", ForgeType.GITLAB, "https://gitlab.com/api/v4")
BASE = "f9defa61fa95bcce4fa2e04b1204ca703d6604a2"
HEAD = "91c0ea6197a51df254ea569d4b5cf700526a9ee5"
MERGE_BASE = "7514ef8caff3feea55c60d628e003ccbda850485"
SHA = "0123456789abcdef0123456789abcdef01234567"


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def replay(name: str, prefix: str = "") -> Callable[[httpx.Request], httpx.Response]:
    """Answer each recorded request (path and query) with its recorded response.

    Paths are compared as sent, below the client's base path ``prefix``.
    """
    exchanges = load_fixture(name)["exchanges"]

    def handler(request: httpx.Request) -> httpx.Response:
        for exchange in exchanges:
            recorded = exchange["request"]
            params = {
                key: value
                for key, value in recorded.get("params", {}).items()
                if isinstance(value, str)
            }
            sent = request.url.raw_path.decode().split("?", 1)[0]
            if sent == prefix + recorded["path"] and dict(request.url.params) == params:
                response = exchange["response"]
                headers = {
                    key: value
                    for key, value in response.get("headers", {}).items()
                    if key.lower() != "content-length"
                }
                if "body_json" in response:
                    return httpx.Response(
                        response["status"], headers=headers, json=response["body_json"]
                    )
                body = response.get("body_text", "recorded body")
                return httpx.Response(
                    response["status"], headers=headers, content=body.encode()
                )
        return httpx.Response(404, json={"message": "Not Found"})

    return handler


def make_http(
    handler: Callable[[httpx.Request], httpx.Response],
    base_url: str = "https://api.github.com",
    *,
    limiter: HostRequestLimiter | None = None,
) -> httpx.AsyncClient:
    """Build a client the way create_client does, over a mock transport."""
    client = httpx.AsyncClient(
        base_url=base_url,
        transport=httpx.MockTransport(handler),
        headers={"Accept": "application/json", "Authorization": "Bearer tok"},
        follow_redirects=True,
    )
    if limiter is not None:
        limiter.install(client)
    return client


class Recorder:
    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.handler = handler
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)


class TestGitHubMergeBase:
    @pytest.mark.asyncio
    async def test_reads_the_merge_base_from_the_paged_compare(self) -> None:
        recorder = Recorder(replay("github_compare_merge_base"))
        async with make_http(recorder) as client:
            merge_base = await GitHubClient(GITHUB_HOST, client).get_merge_base(
                "nodejs/node", BASE, HEAD
            )
        assert merge_base == MERGE_BASE
        assert merge_base != BASE
        assert len(recorder.requests) == 1
        request = recorder.requests[0]
        assert request.method == "GET"
        assert request.url.path == f"/repos/nodejs/node/compare/{BASE}...{HEAD}"
        assert dict(request.url.params) == {"per_page": "1", "page": "2"}

    @pytest.mark.asyncio
    async def test_short_and_identical_compares_still_name_the_merge_base(
        self,
    ) -> None:
        handler = replay("github_compare_edge_cases")
        base = "5fbdfcb8133be2bed88bf3ce62b2335f50474525"
        async with make_http(handler) as client:
            forge = GitHubClient(GITHUB_HOST, client)
            ahead = await forge.get_merge_base(
                "github-linguist/linguist",
                base,
                "4c8de62af1693131afcc568dde2da47244187c8f",
            )
            identical = await forge.get_merge_base(
                "github-linguist/linguist", base, base
            )
        assert ahead == base
        assert identical == base

    @pytest.mark.asyncio
    async def test_unknown_commit_is_not_found(self) -> None:
        handler = replay("github_compare_edge_cases")
        async with make_http(handler) as client:
            with pytest.raises(NotFoundError):
                await GitHubClient(GITHUB_HOST, client).get_merge_base(
                    "github-linguist/linguist",
                    "5fbdfcb8133be2bed88bf3ce62b2335f50474525",
                    "0000000000000000000000000000000000000001",
                )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [
            b"not json",
            b"[]",
            b"{}",
            b'{"merge_base_commit": null}',
            b'{"merge_base_commit": {"sha": "7514ef8"}}',
            json.dumps({"merge_base_commit": {"sha": MERGE_BASE.upper()}}).encode(),
            b'{"merge_base_commit": {"sha": 7}}',
        ],
    )
    async def test_a_body_without_a_full_merge_base_is_not_found(
        self, body: bytes
    ) -> None:
        async with make_http(lambda _r: httpx.Response(200, content=body)) as client:
            with pytest.raises(NotFoundError):
                await GitHubClient(GITHUB_HOST, client).get_merge_base(
                    "acme/widgets", BASE, HEAD
                )

    @pytest.mark.asyncio
    async def test_body_over_the_cap_is_refused(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, headers={"Content-Length": str(MERGE_BASE_MAX_BYTES + 1)}
            )

        async with make_http(handler) as client:
            with pytest.raises(ContentTooLargeError):
                await GitHubClient(GITHUB_HOST, client).get_merge_base(
                    "acme/widgets", BASE, HEAD
                )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("base", "head"),
        [("main", HEAD), (BASE, BASE[:7]), (BASE.upper(), HEAD), (BASE, "../x")],
    )
    async def test_invalid_commits_make_no_request(self, base: str, head: str) -> None:
        recorder = Recorder(lambda _r: httpx.Response(500))
        async with make_http(recorder) as client:
            with pytest.raises(ValidationError):
                await GitHubClient(GITHUB_HOST, client).get_merge_base(
                    "acme/widgets", base, head
                )
        assert recorder.requests == []

    @pytest.mark.asyncio
    async def test_a_renamed_repository_redirect_is_followed(self) -> None:
        recorded = replay("github_redirect_renamed_repo")
        base = "5fbdfcb8133be2bed88bf3ce62b2335f50474525"

        def answer(request: httpx.Request) -> httpx.Response:
            if request.url.path.startswith("/repositories/"):
                return httpx.Response(200, json={"merge_base_commit": {"sha": base}})
            return recorded(request)

        recorder = Recorder(answer)
        async with make_http(recorder) as client:
            merge_base = await GitHubClient(GITHUB_HOST, client).get_merge_base(
                "github/linguist", base, base
            )
        assert merge_base == base
        assert [request.url.path for request in recorder.requests] == [
            f"/repos/github/linguist/compare/{base}...{base}",
            f"/repositories/1725199/compare/{base}...{base}",
        ]
        assert dict(recorder.requests[1].url.params) == {"per_page": "1", "page": "2"}


class TestRevisionBase:
    def test_github_base_is_the_branch_tip(self) -> None:
        forge = GitHubClient(GITHUB_HOST, httpx.AsyncClient())
        assert forge.revision_base_is_merge_base is False
        assert revision_base_is_merge_base(forge) is False

    def test_gitlab_base_is_the_merge_base(self) -> None:
        forge = GitLabClient(GITLAB_HOST, httpx.AsyncClient())
        assert forge.revision_base_is_merge_base is True
        assert revision_base_is_merge_base(forge) is True

    def test_duck_typed_clients_are_not_assumed_to_know(self) -> None:
        class Plain:
            pass

        class Truthy:
            revision_base_is_merge_base = "yes"

        assert revision_base_is_merge_base(Plain()) is False  # type: ignore[arg-type]
        assert revision_base_is_merge_base(Truthy()) is False  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_cached_client_forwards_the_flag(self, store: CacheStore) -> None:
        forge = GitLabClient(GITLAB_HOST, httpx.AsyncClient())
        cached = CachedForgeClient(forge, store, "gitlab.com")
        assert revision_base_is_merge_base(cached) is True  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_clients_without_a_merge_base_read_are_unsupported(self) -> None:
        class Plain:
            pass

        with pytest.raises(UnsupportedFileReadError):
            await read_merge_base(Plain(), "a/b", BASE, HEAD)  # type: ignore[arg-type]
        gitlab = GitLabClient(GITLAB_HOST, httpx.AsyncClient())
        with pytest.raises(UnsupportedFileReadError):
            await read_merge_base(gitlab, "a/b", BASE, HEAD)

    def test_full_commit_sha(self) -> None:
        assert is_full_commit_sha(BASE)
        assert is_full_commit_sha("ab" * 32)
        assert not is_full_commit_sha(BASE[:-1])
        assert not is_full_commit_sha(BASE.upper())
        assert not is_full_commit_sha(None)


class MergeBaseClient:
    """A duck-typed client that answers merge bases and counts calls."""

    def __init__(self, answer: object = MERGE_BASE) -> None:
        self.answer = answer
        self.calls: list[tuple[str, str, str]] = []

    async def get_merge_base(self, repo_path: str, base_sha: str, head_sha: str) -> str:
        self.calls.append((repo_path, base_sha, head_sha))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer  # type: ignore[return-value]


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[CacheStore]:
    cache = CacheStore(db_path=tmp_path / "cache.db", max_size_mb=10)
    await cache.open()
    yield cache
    await cache.close()


def wrap(inner: object, store: CacheStore) -> CachedForgeClient:
    return CachedForgeClient(inner, store, "github.com")  # type: ignore[arg-type]


class TestCachedMergeBase:
    @pytest.mark.asyncio
    async def test_second_read_is_served_from_the_database(
        self, store: CacheStore
    ) -> None:
        inner = MergeBaseClient()
        assert await wrap(inner, store).get_merge_base("o/r", BASE, HEAD) == MERGE_BASE
        assert await wrap(inner, store).get_merge_base("o/r", BASE, HEAD) == MERGE_BASE
        assert len(inner.calls) == 1

    @pytest.mark.asyncio
    async def test_pairs_and_repositories_are_cached_apart(
        self, store: CacheStore
    ) -> None:
        inner = MergeBaseClient()
        cached = wrap(inner, store)
        await cached.get_merge_base("o/r", BASE, HEAD)
        await cached.get_merge_base("o/r", HEAD, BASE)
        await cached.get_merge_base("o/other", BASE, HEAD)
        assert len(inner.calls) == 3

    @pytest.mark.asyncio
    async def test_ttl_is_seven_days(self, store: CacheStore) -> None:
        ttls: list[int] = []
        put = store.put

        async def recording_put(key: str, value: bytes, ttl: int) -> None:
            ttls.append(ttl)
            await put(key, value, ttl)

        store.put = recording_put  # type: ignore[method-assign]
        await wrap(MergeBaseClient(), store).get_merge_base("o/r", BASE, HEAD)
        assert ttls == [MERGE_BASE_TTL] == [7 * 24 * 60 * 60]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "answer", [NotFoundError("Not found"), ContentTooLargeError(10)]
    )
    async def test_errors_are_never_cached(
        self, store: CacheStore, answer: Exception
    ) -> None:
        inner = MergeBaseClient(answer)
        for _ in range(2):
            with pytest.raises(type(answer)):
                await wrap(inner, store).get_merge_base("o/r", BASE, HEAD)
        assert len(inner.calls) == 2

    @pytest.mark.asyncio
    async def test_an_answer_that_is_not_a_full_sha_is_not_cached(
        self, store: CacheStore
    ) -> None:
        inner = MergeBaseClient("7514ef8")
        await wrap(inner, store).get_merge_base("o/r", BASE, HEAD)
        await wrap(inner, store).get_merge_base("o/r", BASE, HEAD)
        assert len(inner.calls) == 2

    @pytest.mark.asyncio
    async def test_refs_other_than_full_shas_are_never_cached(
        self, store: CacheStore
    ) -> None:
        inner = MergeBaseClient()
        await wrap(inner, store).get_merge_base("o/r", "main", HEAD)
        await wrap(inner, store).get_merge_base("o/r", "main", HEAD)
        assert len(inner.calls) == 2

    @pytest.mark.asyncio
    async def test_clearing_the_cache_removes_merge_bases(
        self, store: CacheStore
    ) -> None:
        inner = MergeBaseClient()
        await wrap(inner, store).get_merge_base("o/r", BASE, HEAD)
        await store.clear()
        await wrap(inner, store).get_merge_base("o/r", BASE, HEAD)
        assert len(inner.calls) == 2

    @pytest.mark.asyncio
    async def test_inner_client_without_the_read_is_unsupported(
        self, store: CacheStore
    ) -> None:
        class Plain:
            pass

        with pytest.raises(UnsupportedFileReadError):
            await wrap(Plain(), store).get_merge_base("o/r", BASE, HEAD)


def moved(
    location: str, status: int = 301, *, source: str = "/repos/old/r/contents/a.py"
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == source:
            return httpx.Response(status, headers={"Location": location})
        return httpx.Response(200, content=b"moved body")

    return handler


class TestRedirectRule:
    @pytest.mark.asyncio
    async def test_github_renamed_repository_file_read_is_followed(self) -> None:
        recorder = Recorder(replay("github_redirect_renamed_repo"))
        ref = "5fbdfcb8133be2bed88bf3ce62b2335f50474525"
        async with make_http(recorder) as client:
            body = await GitHubClient(GITHUB_HOST, client).get_file_at_ref(
                "github/linguist", "README.md", ref, max_bytes=1000
            )
        assert body == b"recorded body"
        assert len(recorder.requests) == 2
        hop = recorder.requests[1]
        assert hop.url.host == "api.github.com"
        assert hop.url.path == "/repositories/1725199/contents/README.md"
        assert dict(hop.url.params) == {"ref": ref}
        assert hop.headers["Accept"] == "application/vnd.github.raw"
        assert hop.headers["Authorization"] == "Bearer tok"

    @pytest.mark.asyncio
    async def test_gitlab_renamed_project_file_read_is_followed(self) -> None:
        recorder = Recorder(replay("gitlab_redirect_renamed_project", "/api/v4"))
        async with make_http(recorder, "https://gitlab.com/api/v4") as client:
            body = await request_bytes(
                client,
                "/projects/gitlab-org%2Fgitlab-ce/repository/files/README.md/raw",
                max_bytes=1000,
                params={"ref": "master"},
            )
        assert body == b"recorded body"
        assert recorder.requests[1].url.path == (
            "/api/v4/projects/13083/repository/files/README.md/raw"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [301, 302, 307, 308])
    async def test_same_api_redirects_are_followed(self, status: int) -> None:
        handler = moved("/repos/new/r/contents/a.py?ref=" + SHA, status)
        async with make_http(handler) as client:
            body = await request_bytes(
                client, "/repos/old/r/contents/a.py", max_bytes=100, params={"ref": SHA}
            )
        assert body == b"moved body"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "location",
        [
            "https://objects.example.net/repos/new/r/contents/a.py?ref=" + SHA,
            "http://api.github.com/repos/new/r/contents/a.py?ref=" + SHA,
            "https://api.github.com:8443/repos/new/r/contents/a.py?ref=" + SHA,
            "https://user@api.github.com/repos/new/r/contents/a.py?ref=" + SHA,
            "https://api.github.com/repos/new/r/contents/a.py",
            "https://api.github.com/repos/new/r/contents/a.py?ref=main",
            "https://api.github.com/repos/new/r/contents/a.py?ref=main&ref=" + SHA,
        ],
    )
    async def test_redirects_off_the_api_or_the_commit_are_refused(
        self, location: str
    ) -> None:
        recorder = Recorder(moved(location))
        async with make_http(recorder) as client:
            with pytest.raises(UnsupportedFileReadError) as caught:
                await request_bytes(
                    client,
                    "/repos/old/r/contents/a.py",
                    max_bytes=100,
                    params={"ref": SHA},
                )
        assert "301" in str(caught.value)
        assert len(recorder.requests) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "location",
        [
            "https://gitlab.com/api/v3/projects/1/repository/files/a/raw?ref=" + SHA,
            "https://gitlab.com/projects/1/repository/files/a/raw?ref=" + SHA,
            "https://gitlab.com/api/v4/%2e%2e/v3/files/a/raw?ref=" + SHA,
        ],
    )
    async def test_gitlab_redirects_outside_the_api_path_are_refused(
        self, location: str
    ) -> None:
        recorder = Recorder(moved(location, source="/api/v4/projects/a/raw"))
        async with make_http(recorder, "https://gitlab.com/api/v4") as client:
            with pytest.raises(UnsupportedFileReadError):
                await request_bytes(
                    client, "/projects/a/raw", max_bytes=100, params={"ref": SHA}
                )
        assert len(recorder.requests) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [300, 303, 304, 305])
    async def test_other_redirect_statuses_are_refused(self, status: int) -> None:
        handler = moved("/repos/new/r/contents/a.py", status)
        async with make_http(handler) as client:
            with pytest.raises(UnsupportedFileReadError):
                await request_bytes(client, "/repos/old/r/contents/a.py", max_bytes=100)

    @pytest.mark.asyncio
    async def test_a_redirect_without_a_location_is_refused(self) -> None:
        async with make_http(lambda _r: httpx.Response(301)) as client:
            with pytest.raises(UnsupportedFileReadError):
                await request_bytes(client, "/a", max_bytes=100)

    @pytest.mark.asyncio
    async def test_two_hops_are_followed_and_a_third_is_refused(self) -> None:
        def chain(hops: int) -> Recorder:
            def handler(request: httpx.Request) -> httpx.Response:
                step = int(request.url.path.rsplit("/", 1)[1])
                if step < hops:
                    return httpx.Response(301, headers={"Location": f"/hop/{step + 1}"})
                return httpx.Response(200, content=b"end")

            return Recorder(handler)

        followed = chain(MAX_READ_REDIRECTS)
        async with make_http(followed) as client:
            assert await request_bytes(client, "/hop/0", max_bytes=100) == b"end"
        assert len(followed.requests) == MAX_READ_REDIRECTS + 1

        refused = chain(MAX_READ_REDIRECTS + 1)
        async with make_http(refused) as client:
            with pytest.raises(UnsupportedFileReadError):
                await request_bytes(client, "/hop/0", max_bytes=100)
        assert len(refused.requests) == MAX_READ_REDIRECTS + 1

    @pytest.mark.asyncio
    async def test_each_hop_gives_its_slot_back(self) -> None:
        handler = moved("/repos/new/r/contents/a.py")
        limiter = HostRequestLimiter(1)
        async with make_http(handler, limiter=limiter) as client:
            body = await asyncio.wait_for(
                request_bytes(client, "/repos/old/r/contents/a.py", max_bytes=100), 5
            )
            again = await asyncio.wait_for(
                request_bytes(client, "/repos/new/r/contents/a.py", max_bytes=100), 5
            )
        assert body == again == b"moved body"

    @pytest.mark.asyncio
    async def test_the_followed_body_keeps_the_byte_limit(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/old":
                return httpx.Response(301, headers={"Location": "/new"})
            return httpx.Response(200, content=b"x" * 101)

        async with make_http(handler) as client:
            with pytest.raises(ContentTooLargeError):
                await request_bytes(client, "/old", max_bytes=100)
