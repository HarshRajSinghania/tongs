"""Bounded HTTPS downloads for fixed-repository desktop release assets."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit

import httpx

from tongs.desktop.installer.models import (
    InstallerError,
    InstallerErrorCode,
    InstallerLimits,
    ReleaseAsset,
)

GITHUB_API_ORIGIN = "https://api.github.com"
_ALLOWED_DOWNLOAD_HOSTS = frozenset(
    {
        "api.github.com",
        "github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
        "github-releases.githubusercontent.com",
    }
)
_RATE_LIMIT_MESSAGE = "GitHub's anonymous API rate limit was reached."
_MAX_RATE_LIMIT_WAIT = timedelta(days=1)
_MAX_HEADER_DIGITS = 12


async def download_asset_bytes(
    client: httpx.AsyncClient,
    asset: ReleaseAsset,
    *,
    limits: InstallerLimits,
    maximum_bytes: int,
    expected_sha256: str | None = None,
) -> bytes:
    """Download one validated release asset into a single immutable buffer."""
    if maximum_bytes <= 0:
        raise InstallerError(
            InstallerErrorCode.LIMIT_EXCEEDED,
            "The requested desktop download limit is invalid.",
        )
    if asset.byte_count > maximum_bytes:
        raise InstallerError(
            InstallerErrorCode.LIMIT_EXCEEDED,
            "The desktop release asset exceeds the installer download limit.",
        )
    return await download_bytes(
        client,
        asset.api_url,
        limits=limits,
        maximum_bytes=maximum_bytes,
        expected_byte_count=asset.byte_count,
        expected_sha256=expected_sha256,
        accept="application/octet-stream",
    )


async def download_bytes(
    client: httpx.AsyncClient,
    url: str,
    *,
    limits: InstallerLimits,
    maximum_bytes: int,
    expected_byte_count: int | None = None,
    expected_sha256: str | None = None,
    accept: str = "application/vnd.github+json",
) -> bytes:
    """Stream one HTTPS response with explicit redirects, size and digest bounds."""
    content = await _download(
        client,
        url,
        limits=limits,
        maximum_bytes=maximum_bytes,
        expected_byte_count=expected_byte_count,
        expected_sha256=expected_sha256,
        accept=accept,
        allow_missing=False,
    )
    if content is None:
        raise AssertionError("a required download reported a missing resource")
    return content


async def download_optional_bytes(
    client: httpx.AsyncClient,
    url: str,
    *,
    limits: InstallerLimits,
    maximum_bytes: int,
) -> bytes | None:
    """Stream one bounded JSON response, or return None when its source answers 404."""
    return await _download(
        client,
        url,
        limits=limits,
        maximum_bytes=maximum_bytes,
        expected_byte_count=None,
        expected_sha256=None,
        accept="application/vnd.github+json",
        allow_missing=True,
    )


async def _download(
    client: httpx.AsyncClient,
    url: str,
    *,
    limits: InstallerLimits,
    maximum_bytes: int,
    expected_byte_count: int | None,
    expected_sha256: str | None,
    accept: str,
    allow_missing: bool,
) -> bytes | None:
    if maximum_bytes <= 0:
        raise InstallerError(
            InstallerErrorCode.LIMIT_EXCEEDED,
            "The desktop download limit is invalid.",
        )
    current_url = url
    for redirect_count in range(limits.max_redirects + 1):
        _validate_download_url(current_url)
        request = httpx.Request(
            "GET",
            current_url,
            headers={
                "Accept": accept,
                "User-Agent": "tongs-desktop-installer",
            },
        )
        response: httpx.Response | None = None
        try:
            response = await client.send(
                request,
                stream=True,
                auth=None,
                follow_redirects=False,
            )
            if response.status_code in {301, 302, 303, 307, 308}:
                if redirect_count >= limits.max_redirects:
                    _download_error(
                        "Desktop release download redirected too many times."
                    )
                location = response.headers.get("location")
                if not location:
                    _download_error(
                        "Desktop release download returned an invalid redirect."
                    )
                current_url = urljoin(current_url, location)
                continue
            if response.status_code == 404 and allow_missing:
                return None
            if _is_rate_limited(response):
                raise _rate_limit_error(response.headers)
            if response.status_code != 200:
                _download_error("Desktop release download was rejected by its source.")
            content_encoding = response.headers.get("content-encoding", "identity")
            if content_encoding.lower() not in {"", "identity"}:
                _download_error("Encoded desktop release responses are unsupported.")
            _validate_content_length(
                response.headers.get("content-length"),
                maximum_bytes,
                expected_byte_count,
            )
            return await _consume_response(
                response,
                maximum_bytes=maximum_bytes,
                expected_byte_count=expected_byte_count,
                expected_sha256=expected_sha256,
                chunk_bytes=limits.stream_chunk_bytes,
            )
        except asyncio.CancelledError:
            raise
        except InstallerError:
            raise
        except httpx.TransportError:
            raise InstallerError(
                InstallerErrorCode.DOWNLOAD_FAILED,
                "The desktop release download did not complete.",
                retryable=True,
            ) from None
        finally:
            if response is not None:
                await response.aclose()
    raise AssertionError("redirect loop did not return")


def _is_rate_limited(response: httpx.Response) -> bool:
    if response.status_code == 429:
        return True
    if response.status_code != 403:
        return False
    remaining = response.headers.get("x-ratelimit-remaining")
    return (
        remaining is not None and remaining.strip() == "0"
    ) or "retry-after" in response.headers


def _rate_limit_error(headers: httpx.Headers) -> InstallerError:
    """Name the local reset time from bounded headers, never the response body."""
    now = _utc_now()
    reset = _rate_limit_reset(headers, now)
    if reset is None:
        guidance = "Wait a few minutes, then retry."
    else:
        local = max(reset, now).astimezone()
        guidance = f"Retry after {local.strftime('%Y-%m-%d %H:%M:%S %Z').strip()}."
    return InstallerError(
        InstallerErrorCode.DOWNLOAD_FAILED,
        f"{_RATE_LIMIT_MESSAGE} {guidance}",
        retryable=True,
    )


def _rate_limit_reset(headers: httpx.Headers, now: datetime) -> datetime | None:
    reset: datetime | None = None
    retry_after = headers.get("retry-after")
    if retry_after is not None:
        reset = _retry_after_time(retry_after.strip(), now)
    if reset is None:
        epoch = headers.get("x-ratelimit-reset")
        if epoch is not None and _is_bounded_digits(epoch.strip()):
            try:
                reset = datetime.fromtimestamp(int(epoch.strip()), UTC)
            except (OverflowError, OSError, ValueError):
                reset = None
    if reset is None or reset > now + _MAX_RATE_LIMIT_WAIT:
        return None
    return reset


def _retry_after_time(value: str, now: datetime) -> datetime | None:
    if _is_bounded_digits(value):
        seconds = int(value)
        if seconds > _MAX_RATE_LIMIT_WAIT.total_seconds():
            return None
        return now + timedelta(seconds=seconds)
    if not value.isascii() or len(value) > 64:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _is_bounded_digits(value: str) -> bool:
    return (
        bool(value)
        and value.isascii()
        and value.isdigit()
        and len(value) <= _MAX_HEADER_DIGITS
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


async def _consume_response(
    response: httpx.Response,
    *,
    maximum_bytes: int,
    expected_byte_count: int | None,
    expected_sha256: str | None,
    chunk_bytes: int,
) -> bytes:
    content = bytearray()
    byte_count = 0
    digest = hashlib.sha256()
    async for chunk in response.aiter_bytes(chunk_size=chunk_bytes):
        byte_count += len(chunk)
        if byte_count > maximum_bytes:
            raise InstallerError(
                InstallerErrorCode.LIMIT_EXCEEDED,
                "The desktop release response exceeds the download limit.",
            )
        content.extend(chunk)
        digest.update(chunk)
    if expected_byte_count is not None and byte_count != expected_byte_count:
        raise InstallerError(
            InstallerErrorCode.INTEGRITY_FAILED,
            "The desktop release response length is incorrect.",
        )
    if expected_sha256 is not None and digest.hexdigest() != expected_sha256:
        raise InstallerError(
            InstallerErrorCode.INTEGRITY_FAILED,
            "The desktop release response digest is incorrect.",
        )
    return bytes(content)


def _validate_download_url(url: str) -> None:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        _download_error("Desktop release source URL is invalid.")
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _ALLOWED_DOWNLOAD_HOSTS
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        _download_error("Desktop release source URL is not trusted.")


def _validate_content_length(
    value: str | None, maximum_bytes: int, expected_byte_count: int | None
) -> None:
    if value is None:
        return
    try:
        if not value.isascii() or not value.isdigit():
            raise ValueError
        byte_count = int(value)
    except ValueError:
        _download_error("Desktop release response length is invalid.")
    if byte_count < 0 or byte_count > maximum_bytes:
        raise InstallerError(
            InstallerErrorCode.LIMIT_EXCEEDED,
            "The desktop release response exceeds the download limit.",
        )
    if expected_byte_count is not None and byte_count != expected_byte_count:
        raise InstallerError(
            InstallerErrorCode.INTEGRITY_FAILED,
            "The desktop release response length is incorrect.",
        )


def _download_error(message: str) -> None:
    raise InstallerError(InstallerErrorCode.DOWNLOAD_FAILED, message)


__all__ = [
    "GITHUB_API_ORIGIN",
    "download_asset_bytes",
    "download_bytes",
    "download_optional_bytes",
]
