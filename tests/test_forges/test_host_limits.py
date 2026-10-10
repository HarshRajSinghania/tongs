"""Per-host request bounds and non-blocking token lookup (#304)."""

from __future__ import annotations

import asyncio
import threading
from collections import defaultdict
from collections.abc import Callable
from unittest.mock import patch

import httpx
import pytest

from tongs.errors import AuthError
from tongs.forges.github import GitHubClient
from tongs.forges.gitlab import GitLabClient
from tongs.forges.http import (
    HostRequestLimiter,
    create_client,
    request,
)
from tongs.forges.registry import ForgeRegistry
from tongs.scanner.repo import ForgeType

_MR_COUNT = 50


def _github_pull(number: int) -> dict[str, object]:
    return {
        "number": number,
        "title": f"PR {number}",
        "state": "open",
        "draft": False,
        "user": {"login": "alice"},
        "head": {"ref": "feature", "sha": f"sha-{number}"},
        "base": {"ref": "main", "sha": "base"},
        "created_at": "2026-09-01T00:00:00Z",
        "updated_at": "2026-09-02T00:00:00Z",
        "html_url": f"https://github.com/acme/repo/pull/{number}",
        "labels": [],
    }


def _gitlab_mr(iid: int) -> dict[str, object]:
    return {
        "iid": iid,
        "title": f"MR {iid}",
        "state": "opened",
        "draft": False,
        "source_branch": "feature",
        "target_branch": "main",
        "web_url": f"https://gitlab.com/acme/repo/-/merge_requests/{iid}",
        "created_at": "2026-09-01T00:00:00Z",
        "updated_at": "2026-09-02T00:00:00Z",
        "author": {"username": "alice", "name": "Alice"},
        "labels": [],
        "references": {"full": f"acme/repo!{iid}"},
    }


class _Forge:
    """Answer GitHub and GitLab requests while counting sends per host."""

    def __init__(self) -> None:
        self.active: dict[str, int] = defaultdict(int)
        self.peak: dict[str, int] = defaultdict(int)
        self.total_active = 0
        self.total_peak = 0
        self.paths: dict[str, list[str]] = defaultdict(list)
        self.respond: Callable[[httpx.Request], httpx.Response | None] = (
            lambda _request: None
        )

    async def handle(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        self.paths[host].append(request.url.path)
        self.active[host] += 1
        self.total_active += 1
        self.peak[host] = max(self.peak[host], self.active[host])
        self.total_peak = max(self.total_peak, self.total_active)
        try:
            # Yield a few times so every waiting send gets a chance to start.
            for _ in range(3):
                await asyncio.sleep(0)
            await asyncio.sleep(0.002)
            response = self.respond(request)
            return response if response is not None else self._answer(request)
        finally:
            self.active[host] -= 1
            self.total_active -= 1

    def _answer(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/repos/acme/repo/pulls":
            pulls = [_github_pull(n) for n in range(1, _MR_COUNT + 1)]
            return httpx.Response(200, json=pulls)
        if path.endswith("/check-runs"):
            return httpx.Response(
                200,
                json={"check_runs": [{"status": "completed", "conclusion": "success"}]},
            )
        if path == "/graphql":
            return httpx.Response(200, json={"data": {"viewer": {"login": "a"}}})
        if path.endswith(("/logs", "/trace")):
            return httpx.Response(200, text="log line\n")
        if path == "/api/v4/projects/acme%2Frepo/merge_requests" or path.endswith(
            "/acme/repo/merge_requests"
        ):
            return httpx.Response(
                200, json=[_gitlab_mr(n) for n in range(1, _MR_COUNT + 1)]
            )
        if path.endswith("/pipelines"):
            return httpx.Response(200, json=[])
        if path == "/repos/acme/repo/actions/runs":
            return httpx.Response(200, json={"workflow_runs": []})
        return httpx.Response(200, json={})


@pytest.fixture
def forge(monkeypatch: pytest.MonkeyPatch) -> _Forge:
    """Route every real httpx transport send to the mocked forge."""
    fake = _Forge()

    async def handle_async_request(
        _transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        return await fake.handle(request)

    monkeypatch.setattr(
        httpx.AsyncHTTPTransport, "handle_async_request", handle_async_request
    )
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    return fake


@pytest.mark.asyncio
async def test_fifty_mrs_stay_within_the_bound_across_list_ci_graphql_and_logs(
    forge: _Forge,
) -> None:
    registry = ForgeRegistry(max_parallel=4)
    with patch("tongs.forges.registry.resolve_token", return_value="tok"):
        github = await registry.get_client("github.com")
        gitlab = await registry.get_client("gitlab.com")
    assert isinstance(github, GitHubClient)
    assert isinstance(gitlab, GitLabClient)

    results = await asyncio.gather(
        github.list_mrs("acme/repo"),
        *(github._graphql("query { viewer { login } }") for _ in range(10)),
        *(github.get_job_log("acme/repo", job) for job in range(10)),
        *(github.list_pipelines("acme/repo") for _ in range(5)),
        gitlab.list_mrs("acme/repo"),
        *(gitlab.get_job_log("acme/repo", job) for job in range(10)),
    )
    await registry.close_all()

    assert len(results[0]) == _MR_COUNT
    github_paths = forge.paths["api.github.com"]
    assert github_paths.count("/repos/acme/repo/pulls") == 1
    assert sum(path.endswith("/check-runs") for path in github_paths) == _MR_COUNT
    assert github_paths.count("/graphql") == 10
    assert sum(path.endswith("/logs") for path in github_paths) == 10
    assert sum(path.endswith("/trace") for path in forge.paths["gitlab.com"]) == 10
    # Each host reaches its bound and never passes it, and the hosts are
    # bounded separately rather than sharing one pool of slots.
    assert forge.peak["api.github.com"] == 4
    assert forge.peak["gitlab.com"] == 4
    assert forge.total_peak > 4


@pytest.mark.asyncio
async def test_registry_without_a_bound_leaves_requests_unbounded(
    forge: _Forge,
) -> None:
    registry = ForgeRegistry()
    assert registry.max_parallel is None
    with patch("tongs.forges.registry.resolve_token", return_value="tok"):
        github = await registry.get_client("github.com")
    await github.list_mrs("acme/repo")
    await registry.close_all()
    assert forge.peak["api.github.com"] > 4


@pytest.mark.asyncio
async def test_one_limiter_per_host_is_kept_across_client_recreation(
    forge: _Forge,
) -> None:
    registry = ForgeRegistry(max_parallel=2)
    with patch("tongs.forges.registry.resolve_token", return_value="tok"):
        await registry.get_client("github.com")
        first = registry._limiters["github.com"]
        await registry.close_all()
        await registry.get_client("github.com")
        await registry.get_client("gitlab.com")
    assert registry._limiters["github.com"] is first
    assert registry._limiters["gitlab.com"] is not first
    await registry.close_all()


@pytest.mark.parametrize("bound", [0, -1])
def test_registry_rejects_a_bound_below_one(bound: int) -> None:
    with pytest.raises(ValueError, match="max_parallel must be positive"):
        ForgeRegistry(max_parallel=bound)


@pytest.mark.asyncio
async def test_bound_of_one_with_concurrent_401_refresh_does_not_deadlock(
    forge: _Forge,
) -> None:
    refreshes: list[int] = []
    tokens: list[str] = []

    def refresh() -> str:
        refreshes.append(1)
        return "fresh"

    def respond(request: httpx.Request) -> httpx.Response:
        tokens.append(request.headers["Authorization"])
        if request.headers["Authorization"] == "Bearer expired":
            return httpx.Response(401, json={"message": "401 Unauthorized"})
        return httpx.Response(200, json={"ok": True})

    forge.respond = respond
    client = create_client(
        "https://gitlab.com/api/v4",
        "expired",
        refresh=refresh,
        limiter=HostRequestLimiter(1),
    )
    async with client:
        results = await asyncio.wait_for(
            asyncio.gather(*(request(client, "GET", "/user") for _ in range(5))),
            timeout=5,
        )
    assert results == [{"ok": True}] * 5
    assert refreshes == [1]
    assert forge.peak["gitlab.com"] == 1
    assert tokens.count("Bearer fresh") == 5


@pytest.mark.asyncio
async def test_streamed_download_keeps_its_slot_until_closed(forge: _Forge) -> None:
    limiter = HostRequestLimiter(1)
    client = create_client("https://gitlab.com/api/v4", "tok", limiter=limiter)
    async with client:
        async with client.stream("GET", "/projects/1/jobs/2/trace") as response:
            assert response.status_code == 200
            waiting = asyncio.create_task(client.get("/user"))
            for _ in range(10):
                await asyncio.sleep(0.005)
            # The stream is still open, so the second request has not been sent.
            assert not waiting.done()
            assert forge.paths["gitlab.com"] == ["/api/v4/projects/1/jobs/2/trace"]
        second = await asyncio.wait_for(waiting, timeout=5)
    assert second.status_code == 200
    assert forge.paths["gitlab.com"][-1] == "/api/v4/user"


@pytest.mark.asyncio
async def test_transport_failure_gives_its_slot_back(forge: _Forge) -> None:
    calls = 0

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200, json={})

    forge.respond = respond
    client = create_client(
        "https://gitlab.com/api/v4", "tok", limiter=HostRequestLimiter(1)
    )
    async with client:
        with pytest.raises(httpx.ConnectError):
            await client.get("/user")
        response = await asyncio.wait_for(client.get("/user"), timeout=5)
    assert response.status_code == 200


def test_limiter_bounds_environment_proxy_mounts_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Installing the limiter keeps the proxies httpx reads from the environment."""
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    plain = create_client("https://gitlab.com/api/v4", "tok")
    limited = create_client(
        "https://gitlab.com/api/v4", "tok", limiter=HostRequestLimiter(2)
    )
    url = httpx.URL("https://gitlab.com/api/v4/user")
    plain_transport = plain._transport_for_url(url)
    limited_transport = limited._transport_for_url(url)
    # The proxy mount is still chosen for the URL, and it is the wrapped one.
    assert plain_transport is not plain._transport
    assert limited_transport is not limited._transport
    assert type(limited_transport).__name__ == "_LimitedTransport"
    assert type(limited._transport).__name__ == "_LimitedTransport"
    assert type(plain_transport).__name__ == "AsyncHTTPTransport"


@pytest.mark.parametrize("bound", [0, -3])
def test_limiter_rejects_a_bound_below_one(bound: int) -> None:
    with pytest.raises(ValueError, match="max_parallel must be positive"):
        HostRequestLimiter(bound)


class TestTokenLookup:
    @pytest.mark.asyncio
    async def test_hung_lookup_for_one_host_does_not_delay_another(self) -> None:
        release = threading.Event()
        started = threading.Event()

        def resolve(hostname: str, _forge_type: ForgeType) -> str:
            if hostname == "gitlab.com":
                started.set()
                release.wait(10)
            return f"tok-{hostname}"

        registry = ForgeRegistry()
        with patch("tongs.forges.registry.resolve_token", side_effect=resolve):
            hung = asyncio.create_task(registry.get_client("gitlab.com"))
            await asyncio.to_thread(started.wait, 5)
            github = await asyncio.wait_for(registry.get_client("github.com"), 2)
            assert not hung.done()
            release.set()
            gitlab = await asyncio.wait_for(hung, 5)
        assert isinstance(github, GitHubClient)
        assert isinstance(gitlab, GitLabClient)
        await registry.close_all()

    @pytest.mark.asyncio
    async def test_concurrent_callers_share_one_lookup(self) -> None:
        release = threading.Event()

        def resolve(_hostname: str, _forge_type: ForgeType) -> str:
            release.wait(10)
            return "tok"

        registry = ForgeRegistry()
        with patch(
            "tongs.forges.registry.resolve_token", side_effect=resolve
        ) as resolved:
            callers = [
                asyncio.create_task(registry.get_client("github.com")) for _ in range(5)
            ]
            await asyncio.sleep(0.01)
            release.set()
            clients = await asyncio.wait_for(asyncio.gather(*callers), 5)
        assert resolved.call_count == 1
        assert all(client is clients[0] for client in clients)
        await registry.close_all()

    @pytest.mark.asyncio
    async def test_cancelled_caller_leaves_the_lookup_for_others(self) -> None:
        release = threading.Event()

        def resolve(_hostname: str, _forge_type: ForgeType) -> str:
            release.wait(10)
            return "tok"

        registry = ForgeRegistry()
        with patch(
            "tongs.forges.registry.resolve_token", side_effect=resolve
        ) as resolved:
            cancelled = asyncio.create_task(registry.get_client("github.com"))
            other = asyncio.create_task(registry.get_client("github.com"))
            await asyncio.sleep(0.01)
            cancelled.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cancelled
            release.set()
            client = await asyncio.wait_for(other, 5)
        assert isinstance(client, GitHubClient)
        assert resolved.call_count == 1
        await registry.close_all()

    @pytest.mark.asyncio
    async def test_failed_lookup_is_reused_until_its_ttl_passes(self) -> None:
        now = [100.0]
        registry = ForgeRegistry(clock=lambda: now[0])
        with patch(
            "tongs.forges.registry.resolve_token",
            side_effect=AuthError("No credentials found for github.com"),
        ) as resolved:
            for _ in range(3):
                with pytest.raises(AuthError, match="No credentials found"):
                    await registry.get_client("github.com")
            assert resolved.call_count == 1

            now[0] += 59.9
            with pytest.raises(AuthError):
                await registry.get_client("github.com")
            assert resolved.call_count == 1

            now[0] += 0.2
            with pytest.raises(AuthError):
                await registry.get_client("github.com")
            assert resolved.call_count == 2

    @pytest.mark.asyncio
    async def test_failure_ttl_is_configurable(self) -> None:
        now = [0.0]
        registry = ForgeRegistry(clock=lambda: now[0], auth_failure_ttl=5.0)
        with patch(
            "tongs.forges.registry.resolve_token",
            side_effect=AuthError("No credentials found"),
        ) as resolved:
            with pytest.raises(AuthError):
                await registry.get_client("github.com")
            now[0] = 4.0
            with pytest.raises(AuthError):
                await registry.get_client("github.com")
            assert resolved.call_count == 1
            now[0] = 5.0
            with pytest.raises(AuthError):
                await registry.get_client("github.com")
            assert resolved.call_count == 2

    @pytest.mark.asyncio
    async def test_failure_is_remembered_per_host(self) -> None:
        def resolve(hostname: str, _forge_type: ForgeType) -> str:
            if hostname == "gitlab.com":
                raise AuthError("No credentials found for gitlab.com")
            return "tok"

        registry = ForgeRegistry(clock=lambda: 0.0)
        with patch("tongs.forges.registry.resolve_token", side_effect=resolve):
            with pytest.raises(AuthError):
                await registry.get_client("gitlab.com")
            client = await registry.get_client("github.com")
        assert isinstance(client, GitHubClient)
        assert set(registry._auth_failures) == {"gitlab.com"}
        await registry.close_all()

    @pytest.mark.asyncio
    async def test_forget_auth_failures_retries_the_lookup(self) -> None:
        outcomes: list[object] = [
            AuthError("No credentials found for github.com"),
            AuthError("No credentials found for gitlab.com"),
            "tok",
            AuthError("still missing"),
            "tok",
        ]

        def resolve(_hostname: str, _forge_type: ForgeType) -> str:
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return str(outcome)

        registry = ForgeRegistry(clock=lambda: 0.0)
        with patch(
            "tongs.forges.registry.resolve_token", side_effect=resolve
        ) as resolved:
            with pytest.raises(AuthError):
                await registry.get_client("github.com")
            with pytest.raises(AuthError):
                await registry.get_client("gitlab.com")

            # Forgetting one host leaves the other's failure in place.
            registry.forget_auth_failures("github.com")
            assert isinstance(await registry.get_client("github.com"), GitHubClient)
            with pytest.raises(AuthError, match="gitlab.com"):
                await registry.get_client("gitlab.com")
            assert resolved.call_count == 3

            registry.forget_auth_failures()
            with pytest.raises(AuthError, match="still missing"):
                await registry.get_client("gitlab.com")
            registry.forget_auth_failures()
            assert isinstance(await registry.get_client("gitlab.com"), GitLabClient)
            assert resolved.call_count == 5
        await registry.close_all()

    @pytest.mark.asyncio
    async def test_lookup_forgotten_while_running_records_no_failure(self) -> None:
        release = threading.Event()

        def resolve(_hostname: str, _forge_type: ForgeType) -> str:
            release.wait(10)
            raise AuthError("No credentials found")

        registry = ForgeRegistry(clock=lambda: 0.0)
        with patch("tongs.forges.registry.resolve_token", side_effect=resolve):
            pending = asyncio.create_task(registry.get_client("github.com"))
            await asyncio.sleep(0.01)
            registry.forget_auth_failures("github.com")
            release.set()
            with pytest.raises(AuthError):
                await asyncio.wait_for(pending, 5)
        assert registry._auth_failures == {}

    @pytest.mark.asyncio
    async def test_unexpected_lookup_errors_are_not_remembered(self) -> None:
        registry = ForgeRegistry(clock=lambda: 0.0)
        with patch(
            "tongs.forges.registry.resolve_token", side_effect=OSError("disk")
        ) as resolved:
            for _ in range(2):
                with pytest.raises(OSError, match="disk"):
                    await registry.get_client("github.com")
        assert resolved.call_count == 2
        assert registry._auth_failures == {}
