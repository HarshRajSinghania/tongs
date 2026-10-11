"""Bounded raw file reads at a commit on GitHub and GitLab (#381)."""

from __future__ import annotations

import asyncio
import gzip
import random
from collections.abc import AsyncIterator, Callable

import httpx
import pytest

from tongs.errors import (
    ContentTooLargeError,
    ForgeError,
    NetworkError,
    NotFoundError,
    RateLimitError,
    UnsupportedFileReadError,
)
from tongs.forges import http
from tongs.forges.base import ForgeClient, read_file_at_ref
from tongs.forges.github import GitHubClient
from tongs.forges.gitlab import GitLabClient
from tongs.forges.http import ERROR_BODY_LIMIT, HostRequestLimiter, request_bytes
from tongs.forges.models import ForgeHost
from tongs.scanner.repo import ForgeType

SHA = "0123456789abcdef0123456789abcdef01234567"
GITHUB_HOST = ForgeHost("github.com", ForgeType.GITHUB, "https://api.github.com")
GITLAB_HOST = ForgeHost("gitlab.com", ForgeType.GITLAB, "https://gitlab.com/api/v4")


def make_http(
    handler: Callable[[httpx.Request], object],
    base_url: str = "https://api.github.com",
    *,
    limiter: HostRequestLimiter | None = None,
) -> httpx.AsyncClient:
    """Build a client the way create_client does, over a mock transport."""
    client = httpx.AsyncClient(
        base_url=base_url,
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
        headers={"Accept": "application/json", "Authorization": "Bearer tok"},
        follow_redirects=True,
    )
    if limiter is not None:
        limiter.install(client)
    return client


class _ChunkStream(httpx.AsyncByteStream):
    """Yield fixed chunks, optionally blocking after the first, and record use."""

    def __init__(self, chunks: list[bytes], *, block: bool = False) -> None:
        self.chunks = chunks
        self.block = block
        self.yielded = 0
        self.closed = False
        self.started = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.yielded += 1
            yield chunk
            self.started.set()
            if self.block:
                await asyncio.Event().wait()

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    recorded: list[float] = []

    async def fake_sleep(delay: float) -> None:
        recorded.append(delay)

    monkeypatch.setattr(http, "_sleep", fake_sleep)
    return recorded


PATH_CASES = [
    (
        "src/tongs/app.py",
        "src/tongs/app.py",
        "src%2Ftongs%2Fapp.py",
    ),
    ("docs/a b.md", "docs/a%20b.md", "docs%2Fa%20b.md"),
    ("i18n/über.txt", "i18n/%C3%BCber.txt", "i18n%2F%C3%BCber.txt"),
    ("notes/#1.txt", "notes/%231.txt", "notes%2F%231.txt"),
    ("what?.txt", "what%3F.txt", "what%3F.txt"),
    ("100%.txt", "100%25.txt", "100%25.txt"),
]


class TestForgeUrls:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(("path", "github_path", "_gitlab_path"), PATH_CASES)
    async def test_github_contents_url_query_and_accept(
        self, path: str, github_path: str, _gitlab_path: str
    ) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, content=b"body\n")

        async with make_http(handler) as client:
            forge = GitHubClient(GITHUB_HOST, client)
            body = await forge.get_file_at_ref("acme/widgets", path, SHA, max_bytes=100)
        assert body == b"body\n"
        assert len(seen) == 1
        request = seen[0]
        assert request.method == "GET"
        assert request.url.host == "api.github.com"
        assert request.url.raw_path.decode() == (
            f"/repos/acme/widgets/contents/{github_path}?ref={SHA}"
        )
        assert request.headers["Accept"] == "application/vnd.github.raw"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("path", "_github_path", "gitlab_path"), PATH_CASES)
    async def test_gitlab_raw_file_url_and_query(
        self, path: str, _github_path: str, gitlab_path: str
    ) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, content=b"body\n")

        async with make_http(handler, "https://gitlab.com/api/v4") as client:
            forge = GitLabClient(GITLAB_HOST, client)
            body = await forge.get_file_at_ref(
                "group/sub/widgets", path, SHA, max_bytes=100
            )
        assert body == b"body\n"
        assert len(seen) == 1
        assert seen[0].url.raw_path.decode() == (
            "/api/v4/projects/group%2Fsub%2Fwidgets/repository/files/"
            f"{gitlab_path}/raw?ref={SHA}"
        )

    @pytest.mark.asyncio
    async def test_gitlab_missing_file_is_not_found(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"message": "404 File Not Found"})

        async with make_http(handler, "https://gitlab.com/api/v4") as client:
            forge = GitLabClient(GITLAB_HOST, client)
            with pytest.raises(NotFoundError):
                await forge.get_file_at_ref("acme/widgets", "a.py", SHA, max_bytes=10)


class TestDuckTypedClients:
    @pytest.mark.asyncio
    async def test_base_client_reports_unsupported(self) -> None:
        with pytest.raises(UnsupportedFileReadError):
            await ForgeClient.get_file_at_ref(
                object(),  # type: ignore[arg-type]
                "acme/widgets",
                "a.py",
                SHA,
                max_bytes=10,
            )

    @pytest.mark.asyncio
    async def test_helper_reports_unsupported_for_a_client_without_reads(
        self,
    ) -> None:
        with pytest.raises(UnsupportedFileReadError):
            await read_file_at_ref(
                object(),  # type: ignore[arg-type]
                "acme/widgets",
                "a.py",
                SHA,
                max_bytes=10,
            )


class TestRequestBytes:
    @pytest.mark.asyncio
    async def test_content_length_over_the_limit_is_refused_before_reading(
        self,
    ) -> None:
        stream = _ChunkStream([b"x" * 50])

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, headers={"Content-Length": "5000"}, stream=stream
            )

        async with make_http(handler) as client:
            with pytest.raises(ContentTooLargeError) as caught:
                await request_bytes(client, "/file", max_bytes=100)
        assert caught.value.limit_bytes == 100
        assert stream.yielded == 0
        assert stream.closed

    @pytest.mark.asyncio
    async def test_body_without_length_is_cut_at_the_limit(self) -> None:
        stream = _ChunkStream([b"x" * 60, b"y" * 60, b"z" * 60])

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=stream)

        async with make_http(handler) as client:
            with pytest.raises(ContentTooLargeError):
                await request_bytes(client, "/file", max_bytes=100)
        assert stream.yielded == 2
        assert stream.closed

    @pytest.mark.asyncio
    async def test_body_at_the_limit_is_returned(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"x" * 100)

        async with make_http(handler) as client:
            assert await request_bytes(client, "/file", max_bytes=100) == b"x" * 100

    @pytest.mark.asyncio
    async def test_gzip_body_is_capped_on_decoded_bytes(self) -> None:
        compressed = gzip.compress(b"a" * 5000)
        assert len(compressed) < 100

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, headers={"Content-Encoding": "gzip"}, content=compressed
            )

        async with make_http(handler) as client:
            with pytest.raises(ContentTooLargeError):
                await request_bytes(client, "/file", max_bytes=1000)
            # The same body within the limit decodes in full.
            assert await request_bytes(client, "/file", max_bytes=5000) == (b"a" * 5000)

    @pytest.mark.asyncio
    async def test_read_asks_for_an_identity_body(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, content=b"body")

        async with make_http(handler) as client:
            await request_bytes(
                client, "/file", max_bytes=100, headers={"Accept": "text/plain"}
            )
            forge = GitHubClient(GITHUB_HOST, client)
            await forge.get_file_at_ref("acme/widgets", "a.py", SHA, max_bytes=100)
        assert [r.headers.get_list("Accept-Encoding") for r in seen] == [
            ["identity"],
            ["identity"],
        ]
        assert seen[0].headers["Accept"] == "text/plain"
        assert seen[1].headers["Accept"] == "application/vnd.github.raw"

    @pytest.mark.asyncio
    async def test_encoded_length_over_the_limit_is_not_refused_early(
        self,
    ) -> None:
        raw = random.Random(0).randbytes(90)
        compressed = gzip.compress(raw)
        # Incompressible bytes grow under gzip, past the decoded-byte limit.
        assert len(compressed) > 100

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={
                    "Content-Encoding": "gzip",
                    "Content-Length": str(len(compressed)),
                },
                content=compressed,
            )

        async with make_http(handler) as client:
            assert await request_bytes(client, "/file", max_bytes=100) == raw

    @pytest.mark.asyncio
    async def test_redirect_is_not_followed(self) -> None:
        hosts: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            hosts.append(request.url.host)
            return httpx.Response(
                302, headers={"Location": "https://objects.example.net/blob"}
            )

        async with make_http(handler) as client:
            with pytest.raises(UnsupportedFileReadError) as caught:
                await request_bytes(client, "/file", max_bytes=100)
        assert hosts == ["api.github.com"]
        assert "302" in str(caught.value)

    @pytest.mark.asyncio
    async def test_error_body_read_is_bounded_and_redacted(self) -> None:
        chunk = b'{"message": "ghp_secrettoken123 broke"' + b" " * (16 * 1024)
        stream = _ChunkStream([chunk] * 64)

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, stream=stream)

        async with make_http(handler) as client:
            with pytest.raises(ForgeError) as caught:
                await request_bytes(client, "/file", max_bytes=100)
        assert str(caught.value).startswith("HTTP 500")
        assert "ghp_secrettoken123" not in str(caught.value)
        assert stream.yielded * len(chunk) <= ERROR_BODY_LIMIT + len(chunk)
        assert stream.closed

    @pytest.mark.asyncio
    async def test_rate_limit_is_retried_once(self, sleeps: list[float]) -> None:
        calls = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(429, headers={"Retry-After": "2"})
            return httpx.Response(200, content=b"ok")

        async with make_http(handler) as client:
            assert await request_bytes(client, "/file", max_bytes=10) == b"ok"
        assert calls == 2
        assert sleeps == [2]

    @pytest.mark.asyncio
    async def test_second_rate_limit_raises(self, sleeps: list[float]) -> None:
        calls = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(429, headers={"Retry-After": "1"})

        async with make_http(handler) as client:
            with pytest.raises(RateLimitError):
                await request_bytes(client, "/file", max_bytes=10)
        assert calls == 2
        assert sleeps == [1]

    @pytest.mark.asyncio
    async def test_long_rate_limit_is_not_retried(self, sleeps: list[float]) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, headers={"Retry-After": "3600"})

        async with make_http(handler) as client:
            with pytest.raises(RateLimitError):
                await request_bytes(client, "/file", max_bytes=10)
        assert sleeps == []

    @pytest.mark.asyncio
    async def test_not_found_is_mapped(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"message": "Not Found"})

        async with make_http(handler) as client:
            with pytest.raises(NotFoundError):
                await request_bytes(client, "/file", max_bytes=10)

    @pytest.mark.asyncio
    async def test_transport_error_is_redacted_network_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused for ghp_secrettoken123", request=request)

        async with make_http(handler) as client:
            with pytest.raises(NetworkError) as caught:
                await request_bytes(client, "/file", max_bytes=10)
        assert "ghp_secrettoken123" not in str(caught.value)

    @pytest.mark.asyncio
    async def test_timeout_is_network_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow", request=request)

        async with make_http(handler) as client:
            with pytest.raises(NetworkError):
                await request_bytes(client, "/file", max_bytes=10)

    @pytest.mark.asyncio
    async def test_negative_limit_is_refused(self) -> None:
        async with make_http(lambda _r: httpx.Response(200)) as client:
            with pytest.raises(ValueError):
                await request_bytes(client, "/file", max_bytes=-1)


class TestHostSlotRelease:
    """With one slot per host, every failed read must give its slot back."""

    @pytest.mark.asyncio
    async def test_slot_is_released_after_a_cap_abort(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/big":
                return httpx.Response(200, stream=_ChunkStream([b"x" * 200]))
            return httpx.Response(200, content=b"ok")

        limiter = HostRequestLimiter(1)
        async with make_http(handler, limiter=limiter) as client:
            with pytest.raises(ContentTooLargeError):
                await request_bytes(client, "/big", max_bytes=100)
            body = await asyncio.wait_for(
                request_bytes(client, "/small", max_bytes=100), 5
            )
        assert body == b"ok"

    @pytest.mark.asyncio
    async def test_slot_is_released_after_a_mid_stream_cancellation(self) -> None:
        stream = _ChunkStream([b"x" * 10, b"y" * 10], block=True)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/slow":
                return httpx.Response(200, stream=stream)
            return httpx.Response(200, content=b"ok")

        limiter = HostRequestLimiter(1)
        async with make_http(handler, limiter=limiter) as client:
            task = asyncio.create_task(request_bytes(client, "/slow", max_bytes=100))
            await asyncio.wait_for(stream.started.wait(), 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            body = await asyncio.wait_for(
                request_bytes(client, "/small", max_bytes=100), 5
            )
        assert body == b"ok"
        assert stream.closed

    @pytest.mark.asyncio
    async def test_slot_is_released_after_an_error_status(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/broken":
                return httpx.Response(500, stream=_ChunkStream([b"oops"] * 4))
            return httpx.Response(200, content=b"ok")

        limiter = HostRequestLimiter(1)
        async with make_http(handler, limiter=limiter) as client:
            with pytest.raises(ForgeError):
                await request_bytes(client, "/broken", max_bytes=100)
            body = await asyncio.wait_for(
                request_bytes(client, "/small", max_bytes=100), 5
            )
        assert body == b"ok"

    @pytest.mark.asyncio
    async def test_slot_is_released_after_a_redirect(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/moved":
                return httpx.Response(
                    302, headers={"Location": "https://objects.example.net/blob"}
                )
            return httpx.Response(200, content=b"ok")

        limiter = HostRequestLimiter(1)
        async with make_http(handler, limiter=limiter) as client:
            with pytest.raises(UnsupportedFileReadError):
                await request_bytes(client, "/moved", max_bytes=100)
            body = await asyncio.wait_for(
                request_bytes(client, "/small", max_bytes=100), 5
            )
        assert body == b"ok"
