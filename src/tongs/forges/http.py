"""Async HTTP transport layer for forge API calls."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import AsyncIterator, Callable
from email.utils import parsedate_to_datetime

import httpx

from tongs.errors import (
    AuthError,
    ConflictError,
    ForgeError,
    ForgePermissionError,
    NetworkError,
    NotFoundError,
    RateLimitError,
    ValidationError,
    redact_credentials,
)

log = logging.getLogger(__name__)

#: The longest rate-limit wait a request sleeps through before its one retry.
#: A forge that asks for a longer wait gets a RateLimitError straight away.
MAX_RETRY_AFTER_SECONDS = 60

#: The wait before the one retry when a rate limit names no reset time.
DEFAULT_RETRY_AFTER_SECONDS = 5

_MAX_HEADER_DIGITS = 12

# Retry sleeps go through this name so tests can observe them without waiting.
_sleep = asyncio.sleep
#: Methods safe to send twice. A rate-limited write fails at once instead of
#: sleeping and resending, so callers with their own deadline (review
#: submission) report the rate limit rather than an unknown outcome.
_RETRYABLE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class HostRequestLimiter:
    """Bound the forge requests in flight to one host.

    Every transport wrapped by one limiter shares its slots. A slot is taken
    when a request is sent and given back when its response stream closes, so
    a streamed download keeps its slot until it is read or closed. The wait
    for a slot has no timeout of its own; the request timeout starts once the
    slot is held.
    """

    def __init__(self, max_parallel: int) -> None:
        if max_parallel < 1:
            raise ValueError("max_parallel must be positive")
        self._max_parallel = max_parallel
        self._semaphore = asyncio.Semaphore(max_parallel)

    @property
    def max_parallel(self) -> int:
        """The most requests this limiter lets run at once."""
        return self._max_parallel

    def wrap(self, transport: httpx.AsyncBaseTransport) -> httpx.AsyncBaseTransport:
        """Return ``transport`` bounded by this limiter's slots."""
        return _LimitedTransport(transport, self._semaphore)

    def install(self, client: httpx.AsyncClient) -> None:
        """Bound every transport ``client`` can send through.

        Passing ``transport=`` to the client constructor would turn off the
        proxies httpx reads from the environment, so the client is built as
        usual and its transport and proxy mounts are wrapped afterwards.
        """
        client._transport = self.wrap(client._transport)
        client._mounts = {
            pattern: None if transport is None else self.wrap(transport)
            for pattern, transport in client._mounts.items()
        }


class _LimitedTransport(httpx.AsyncBaseTransport):
    """Hold one limiter slot from send until the response stream closes."""

    def __init__(
        self, transport: httpx.AsyncBaseTransport, semaphore: asyncio.Semaphore
    ) -> None:
        self._transport = transport
        self._semaphore = semaphore

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await self._semaphore.acquire()
        slot = _Slot(self._semaphore)
        try:
            response = await self._transport.handle_async_request(request)
        except BaseException:
            slot.release()
            raise
        stream = response.stream
        if not isinstance(stream, httpx.AsyncByteStream):
            slot.release()
            raise TypeError("The forge transport returned a synchronous stream")
        return httpx.Response(
            status_code=response.status_code,
            headers=response.headers,
            stream=_SlotStream(stream, slot),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self._transport.aclose()


class _Slot:
    """Release a limiter slot exactly once."""

    def __init__(self, semaphore: asyncio.Semaphore) -> None:
        self._semaphore: asyncio.Semaphore | None = semaphore

    def release(self) -> None:
        semaphore, self._semaphore = self._semaphore, None
        if semaphore is not None:
            semaphore.release()


class _SlotStream(httpx.AsyncByteStream):
    def __init__(self, stream: httpx.AsyncByteStream, slot: _Slot) -> None:
        self._stream = stream
        self._slot = slot

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._stream:
            yield chunk

    async def aclose(self) -> None:
        try:
            await self._stream.aclose()
        finally:
            self._slot.release()


class RefreshingTokenAuth(httpx.Auth):
    """Bearer auth that re-resolves the token once when a request gets a 401.

    Short-lived credentials (such as glab OAuth tokens) expire during long
    sessions. ``refresh`` is a blocking callable returning a new token, or
    ``None`` when no better credential is available; it runs in a worker thread.
    """

    def __init__(self, token: str, refresh: Callable[[], str | None]):
        self._token = token
        self._refresh = refresh
        self._lock = asyncio.Lock()

    def sync_auth_flow(self, request: httpx.Request):
        raise RuntimeError("RefreshingTokenAuth only supports async clients")

    async def async_auth_flow(self, request: httpx.Request):
        sent_token = self._token
        request.headers["Authorization"] = f"Bearer {sent_token}"
        response = yield request
        if response.status_code != 401:
            return

        # Read and close the rejected response first, so it gives back its
        # host request slot before the refresh runs.
        await response.aread()
        async with self._lock:
            # Another request may already have refreshed the token.
            if self._token == sent_token:
                try:
                    new_token = await asyncio.to_thread(self._refresh)
                except ForgeError as e:
                    log.debug("Token refresh failed: %s", e)
                    new_token = None
                if not new_token or new_token == sent_token:
                    return
                log.info("Retrying request with refreshed credentials")
                self._token = new_token

        request.headers["Authorization"] = f"Bearer {self._token}"
        yield request


def create_client(
    base_url: str,
    token: str,
    timeout: float = 30.0,
    refresh: Callable[[], str | None] | None = None,
    *,
    limiter: HostRequestLimiter | None = None,
) -> httpx.AsyncClient:
    """Create an authenticated async HTTP client for a forge API.

    When ``refresh`` is given, a 401 triggers one token refresh and retry.
    When ``limiter`` is given, every request the client sends, including
    redirects and the retry after a refresh, takes one of its slots.
    """
    headers = {"Accept": "application/json"}
    auth = None
    if refresh is None:
        headers["Authorization"] = f"Bearer {token}"
    else:
        auth = RefreshingTokenAuth(token, refresh)
    client = httpx.AsyncClient(
        base_url=base_url,
        auth=auth,
        headers=headers,
        timeout=timeout,
        follow_redirects=True,
    )
    if limiter is not None:
        limiter.install(client)
    return client


def map_http_error(
    response: httpx.Response, *, now: Callable[[], float] = time.time
) -> ForgeError:
    """Map an HTTP error response to the appropriate ForgeError subclass.

    A 429, and a 403 that carries ``Retry-After`` or ``x-ratelimit-remaining:
    0``, map to RateLimitError. ``now`` returns the current Unix time and
    turns an HTTP-date ``Retry-After`` or an ``x-ratelimit-reset`` epoch into
    a wait in seconds.
    """
    status = response.status_code
    body = _safe_body(response)
    headers = httpx.Headers(response.headers)

    if status == 401:
        return AuthError(f"Authentication failed: {body}")
    if status == 403 and _is_rate_limited_403(headers):
        return RateLimitError(
            f"Rate limited: {body}", retry_after=_retry_after(headers, now)
        )
    if status == 403:
        return ForgePermissionError(f"Insufficient permissions: {body}")
    if status == 404:
        return NotFoundError(f"Not found: {body}")
    if status == 405:
        # GitHub's PR merge endpoint answers a merge attempt it will not
        # perform (merge conflicts, required checks unmet, etc.) with 405
        # and a reason in the body, e.g. {"message": "Pull Request has merge
        # conflicts"}. GitLab's merge endpoint uses the same status when
        # merge_request.mergeable? is false (conflicts, draft, unresolved
        # threads, a failing or pending pipeline), decided before any merge
        # is attempted. Both are a definitive, known-state rejection rather
        # than an ambiguous failure.
        return ConflictError(f"Conflict: {body}")
    if status == 406:
        # Legacy GitLab releases returned 406 on the merge endpoint itself
        # for "branch cannot be merged"; current GitLab reserves 406 for
        # cancel_merge_when_pipeline_succeeds (no auto-merge to cancel).
        # Neither case runs a mutation before returning 406, so it stays a
        # definitive, known-state rejection like 405.
        return ConflictError(f"Conflict: {body}")
    if status == 409:
        return ConflictError(f"Conflict: {body}")
    if status in {400, 422}:
        return ValidationError(f"Validation failed: {body}")
    if status == 429:
        return RateLimitError(
            f"Rate limited: {body}", retry_after=_retry_after(headers, now)
        )

    return ForgeError(f"HTTP {status}: {body}")


def _is_rate_limited_403(headers: httpx.Headers) -> bool:
    """Tell a rate-limit 403 from a permission 403.

    GitHub answers both an exhausted primary rate limit and a secondary rate
    limit with 403; the first sets ``x-ratelimit-remaining: 0`` and the second
    sets ``Retry-After``.
    """
    if headers.get("Retry-After") is not None:
        return True
    remaining = headers.get("x-ratelimit-remaining")
    return remaining is not None and remaining.strip() == "0"


def _retry_after(headers: httpx.Headers, now: Callable[[], float]) -> int | None:
    """Return the wait the forge asked for in whole seconds, or ``None``.

    ``Retry-After`` is read as delta-seconds or as an HTTP-date. Without it, a
    GitHub ``x-ratelimit-reset`` epoch is used.
    """
    retry_after = headers.get("Retry-After")
    if retry_after is not None:
        value = retry_after.strip()
        if _is_bounded_digits(value):
            return int(value)
        return _seconds_until_date(value, now)
    reset = headers.get("x-ratelimit-reset")
    if reset is not None and _is_bounded_digits(reset.strip()):
        return max(0, math.ceil(int(reset.strip()) - now()))
    return None


def _seconds_until_date(value: str, now: Callable[[], float]) -> int | None:
    if not value or not value.isascii() or len(value) > 64:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if parsed.tzinfo is None:
        return None
    return max(0, math.ceil(parsed.timestamp() - now()))


def _is_bounded_digits(value: str) -> bool:
    return (
        bool(value)
        and value.isascii()
        and value.isdigit()
        and len(value) <= _MAX_HEADER_DIGITS
    )


async def request(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    _retried: bool = False,
    **kwargs,
) -> dict | list:
    """Make an authenticated API request with error mapping.

    Returns parsed JSON response body.
    Raises appropriate ForgeError subclass on failure.
    On a rate limit, a GET, HEAD or OPTIONS request waits ``retry_after``
    seconds and retries once, unless the wait is longer than
    ``MAX_RETRY_AFTER_SECONDS``. A rate-limited write raises at once. A timeout
    is not retried.
    """
    response = await _send(client, method, path, _retried=_retried, **kwargs)
    if response.status_code == 204:
        return {}
    return response.json()


async def request_page(
    client: httpx.AsyncClient,
    path: str,
    *,
    page: int,
    per_page: int,
    params: dict[str, str | int] | None = None,
) -> tuple[list[dict], bool]:
    """Read exactly one page of a list endpoint.

    Returns the page's items and whether the forge reports another page. The
    ``Link`` header (GitHub and GitLab) and GitLab's ``X-Next-Page`` header are
    authoritative when present; without either, a full page is taken to mean
    another page may follow, the same rule ``paginate`` uses.
    """
    query: dict[str, str | int] = dict(params or {})
    query["per_page"] = per_page
    query["page"] = page
    response = await _send(client, "GET", path, params=query)
    data = [] if response.status_code == 204 else response.json()
    if not isinstance(data, list):
        raise ValidationError("Validation failed: expected a list response")
    return data, _has_next_page(response, len(data), per_page)


def _has_next_page(response: httpx.Response, count: int, per_page: int) -> bool:
    next_page = response.headers.get("X-Next-Page")
    if next_page is not None:
        return bool(next_page.strip())
    link = response.headers.get("Link")
    if link is not None:
        return any(
            'rel="next"' in part.replace(" ", "") or "rel=next" in part
            for part in link.split(",")
        )
    return count >= per_page


async def _send(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    _retried: bool = False,
    **kwargs,
) -> httpx.Response:
    try:
        response = await client.request(method, path, **kwargs)
    except httpx.TimeoutException as e:
        raise NetworkError(f"Request timed out: {redact_credentials(str(e))}") from e
    except httpx.TransportError as e:
        raise NetworkError(f"Transport error: {redact_credentials(str(e))}") from e

    if response.status_code >= 400:
        err = map_http_error(response)
        if (
            isinstance(err, RateLimitError)
            and not _retried
            and method.upper() in _RETRYABLE_METHODS
        ):
            delay = (
                err.retry_after
                if err.retry_after is not None
                else DEFAULT_RETRY_AFTER_SECONDS
            )
            if delay > MAX_RETRY_AFTER_SECONDS:
                log.warning(
                    "Rate limited on %s %s for %ds; not retrying", method, path, delay
                )
                raise err
            log.warning("Rate limited on %s %s, retrying in %ds", method, path, delay)
            # The response is already read and closed, so no host request slot
            # is held while sleeping.
            await _sleep(delay)
            return await _send(client, method, path, _retried=True, **kwargs)
        raise err

    return response


async def paginate(
    client: httpx.AsyncClient,
    path: str,
    per_page: int = 100,
    max_pages: int | None = None,
    **kwargs,
) -> list[dict]:
    """Paginate a GET request, collecting all results."""
    params = dict(kwargs.pop("params", {}))
    params["per_page"] = per_page

    results: list[dict] = []
    page = 1

    while True:
        params["page"] = page
        data = await request(client, "GET", path, params=params, **kwargs)

        if isinstance(data, list):
            results.extend(data)
            if len(data) < per_page:
                break
        else:
            results.append(data)
            break

        page += 1
        if max_pages and page > max_pages:
            break

    return results


def _safe_body(response: httpx.Response) -> str:
    """Extract a safe, redacted body string from a response."""
    try:
        data = response.json()
        msg = data.get("message", data.get("error", str(data)))
    except (ValueError, AttributeError):
        msg = response.text[:200]
    return redact_credentials(str(msg))
