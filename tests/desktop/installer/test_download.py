"""Bounded fixed-origin release download tests."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta

import httpx
import pytest

import tongs.desktop.installer.download as download_module
from tongs.desktop.installer.download import download_bytes, download_optional_bytes
from tongs.desktop.installer.models import (
    InstallerError,
    InstallerErrorCode,
    InstallerLimits,
)


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes], gate: asyncio.Event | None = None) -> None:
        self.chunks = chunks
        self.gate = gate

    async def __aiter__(self):
        for chunk in self.chunks:
            if self.gate is not None:
                await self.gate.wait()
            yield chunk


@pytest.mark.asyncio
async def test_streams_exact_body_without_forwarding_client_auth() -> None:
    body = b"verified bytes"
    seen_authorization: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_authorization.append(request.headers.get("authorization"))
        return httpx.Response(200, content=body)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        auth=("private-user", "private-token"),
    ) as client:
        result = await download_bytes(
            client,
            "https://api.github.com/release",
            limits=InstallerLimits(),
            maximum_bytes=100,
            expected_byte_count=len(body),
            expected_sha256=hashlib.sha256(body).hexdigest(),
        )

    assert result == body
    assert seen_authorization == [None]


@pytest.mark.asyncio
@pytest.mark.parametrize("follow_redirects", [False, True])
async def test_rejects_redirect_to_untrusted_origin_before_request(
    follow_redirects: bool,
) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://evil.test/archive"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=follow_redirects
    ) as client:
        with pytest.raises(InstallerError) as raised:
            await download_bytes(
                client,
                "https://api.github.com/release",
                limits=InstallerLimits(),
                maximum_bytes=100,
            )

    assert raised.value.code is InstallerErrorCode.DOWNLOAD_FAILED
    assert seen == ["https://api.github.com/release"]


@pytest.mark.asyncio
@pytest.mark.parametrize("follow_redirects", [False, True])
async def test_redirects_are_manually_bounded_regardless_of_client_default(
    follow_redirects: bool,
) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": "/next"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=follow_redirects
    ) as client:
        with pytest.raises(InstallerError) as raised:
            await download_bytes(
                client,
                "https://api.github.com/release",
                limits=InstallerLimits(max_redirects=1),
                maximum_bytes=100,
            )

    assert raised.value.code is InstallerErrorCode.DOWNLOAD_FAILED
    assert seen == [
        "https://api.github.com/release",
        "https://api.github.com/next",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "chunks", "expected"),
    [
        ({"content-length": "11"}, [b"12345678901"], InstallerErrorCode.LIMIT_EXCEEDED),
        ({}, [b"12345"], InstallerErrorCode.INTEGRITY_FAILED),
        ({"content-encoding": "gzip"}, [b"x"], InstallerErrorCode.DOWNLOAD_FAILED),
    ],
)
async def test_oversized_truncated_and_encoded_responses_are_rejected(
    headers: dict[str, str],
    chunks: list[bytes],
    expected: InstallerErrorCode,
) -> None:
    response = lambda _request: httpx.Response(
        200, headers=headers, stream=ChunkStream(chunks)
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        with pytest.raises(InstallerError) as raised:
            await download_bytes(
                client,
                "https://api.github.com/release",
                limits=InstallerLimits(),
                maximum_bytes=10,
                expected_byte_count=10,
            )

    assert raised.value.code is expected


@pytest.mark.asyncio
async def test_cancellation_propagates_from_blocked_stream() -> None:
    gate = asyncio.Event()
    response = lambda _request: httpx.Response(
        200, stream=ChunkStream([b"content"], gate)
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        task = asyncio.create_task(
            download_bytes(
                client,
                "https://api.github.com/release",
                limits=InstallerLimits(),
                maximum_bytes=100,
            )
        )
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_transport_timeout_is_safe_and_retryable() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("remote details", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
        with pytest.raises(InstallerError) as raised:
            await download_bytes(
                client,
                "https://api.github.com/release",
                limits=InstallerLimits(),
                maximum_bytes=100,
            )

    assert raised.value.code is InstallerErrorCode.DOWNLOAD_FAILED
    assert raised.value.retryable is True
    assert "remote details" not in str(raised.value)


@pytest.mark.asyncio
async def test_wrong_digest_is_rejected_after_exact_transfer() -> None:
    body = b"complete but wrong"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, content=body)
        )
    ) as client:
        with pytest.raises(InstallerError) as raised:
            await download_bytes(
                client,
                "https://api.github.com/release",
                limits=InstallerLimits(),
                maximum_bytes=100,
                expected_byte_count=len(body),
                expected_sha256="0" * 64,
            )

    assert raised.value.code is InstallerErrorCode.INTEGRITY_FAILED


@pytest.mark.asyncio
async def test_stream_without_content_length_still_enforces_maximum_bytes() -> None:
    response = lambda _request: httpx.Response(
        200, stream=ChunkStream([b"123456", b"78901"])
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        with pytest.raises(InstallerError) as raised:
            await download_bytes(
                client,
                "https://api.github.com/release",
                limits=InstallerLimits(),
                maximum_bytes=10,
            )

    assert raised.value.code is InstallerErrorCode.LIMIT_EXCEEDED


RATE_NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
PRIVATE_BODY = "API rate limit exceeded for 203.0.113.9 private-detail"


def _local(moment: datetime) -> str:
    return moment.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z").strip()


async def _rejection(
    monkeypatch: pytest.MonkeyPatch, status: int, headers: dict[str, str]
) -> InstallerError:
    monkeypatch.setattr(download_module, "_utc_now", lambda: RATE_NOW)
    response = lambda _request: httpx.Response(
        status, headers=headers, json={"message": PRIVATE_BODY}
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        with pytest.raises(InstallerError) as raised:
            await download_bytes(
                client,
                "https://api.github.com/release",
                limits=InstallerLimits(),
                maximum_bytes=100,
            )
    assert raised.value.code is InstallerErrorCode.DOWNLOAD_FAILED
    assert PRIVATE_BODY not in str(raised.value)
    assert "203.0.113.9" not in str(raised.value)
    return raised.value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "headers", "reset"),
    [
        (
            403,
            {
                "x-ratelimit-remaining": "0",
                "x-ratelimit-reset": str(
                    int((RATE_NOW + timedelta(minutes=17)).timestamp())
                ),
            },
            RATE_NOW + timedelta(minutes=17),
        ),
        (403, {"retry-after": "90"}, RATE_NOW + timedelta(seconds=90)),
        (
            403,
            {"retry-after": "Mon, 07 Sep 2026 12:30:00 GMT"},
            RATE_NOW + timedelta(minutes=30),
        ),
        (429, {"retry-after": "60"}, RATE_NOW + timedelta(seconds=60)),
        (
            429,
            {
                "x-ratelimit-reset": str(
                    int((RATE_NOW + timedelta(hours=1)).timestamp())
                )
            },
            RATE_NOW + timedelta(hours=1),
        ),
    ],
)
async def test_rate_limits_are_retryable_and_name_the_local_reset_time(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    headers: dict[str, str],
    reset: datetime,
) -> None:
    error = await _rejection(monkeypatch, status, headers)

    assert error.retryable is True
    assert str(error) == (
        f"GitHub's anonymous API rate limit was reached. Retry after {_local(reset)}."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"retry-after": "not a time"},
        {"retry-after": "9" * 40},
        {"x-ratelimit-reset": "-5"},
        {"x-ratelimit-reset": str(int((RATE_NOW + timedelta(days=2)).timestamp()))},
    ],
)
async def test_429_without_a_usable_reset_time_is_still_retryable(
    monkeypatch: pytest.MonkeyPatch, headers: dict[str, str]
) -> None:
    error = await _rejection(monkeypatch, 429, headers)

    assert error.retryable is True
    assert str(error) == (
        "GitHub's anonymous API rate limit was reached. Wait a few minutes, then retry."
    )


@pytest.mark.asyncio
async def test_reset_time_already_passed_is_reported_as_now(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    past = int((RATE_NOW - timedelta(minutes=3)).timestamp())
    error = await _rejection(
        monkeypatch,
        403,
        {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(past)},
    )

    assert error.retryable is True
    assert f"Retry after {_local(RATE_NOW)}." in str(error)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [{}, {"x-ratelimit-remaining": "12", "x-ratelimit-reset": "1788000000"}],
)
async def test_plain_403_stays_a_permanent_rejection(
    monkeypatch: pytest.MonkeyPatch, headers: dict[str, str]
) -> None:
    error = await _rejection(monkeypatch, 403, headers)

    assert error.retryable is False
    assert str(error) == "Desktop release download was rejected by its source."


@pytest.mark.asyncio
async def test_optional_download_returns_none_only_for_404() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/missing":
            return httpx.Response(404, json={"message": PRIVATE_BODY})
        if request.url.path == "/gone":
            return httpx.Response(410)
        return httpx.Response(200, content=b"{}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        missing = await download_optional_bytes(
            client,
            "https://api.github.com/missing",
            limits=InstallerLimits(),
            maximum_bytes=100,
        )
        present = await download_optional_bytes(
            client,
            "https://api.github.com/present",
            limits=InstallerLimits(),
            maximum_bytes=100,
        )
        with pytest.raises(InstallerError) as gone:
            await download_optional_bytes(
                client,
                "https://api.github.com/gone",
                limits=InstallerLimits(),
                maximum_bytes=100,
            )
        with pytest.raises(InstallerError) as required:
            await download_bytes(
                client,
                "https://api.github.com/missing",
                limits=InstallerLimits(),
                maximum_bytes=100,
            )

    assert missing is None
    assert present == b"{}"
    assert gone.value.code is InstallerErrorCode.DOWNLOAD_FAILED
    assert required.value.code is InstallerErrorCode.DOWNLOAD_FAILED
    assert required.value.retryable is False
