"""Config-driven registry mapping hostnames to ForgeClient instances."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from functools import partial

from tongs.errors import AuthError
from tongs.forges.auth import refresh_token, resolve_token
from tongs.forges.base import ForgeClient
from tongs.forges.http import HostRequestLimiter, create_client
from tongs.forges.models import ForgeHost
from tongs.scanner.repo import ForgeType


def _github_api_base(hostname: str) -> str:
    if hostname == "github.com":
        return "https://api.github.com"
    return f"https://{hostname}/api/v3"


def _gitlab_api_base(hostname: str) -> str:
    return f"https://{hostname}/api/v4"


#: How long a failed token lookup for a host is reused before it is retried.
AUTH_FAILURE_TTL_SECONDS = 60.0


class ForgeRegistry:
    """Maps hostnames to authenticated ForgeClient instances.

    Clients are created lazily on first access and cached for reuse.

    ``max_parallel`` bounds the requests in flight to each forge host across
    every client, call path and caller of this registry; ``None`` leaves them
    unbounded. Token lookups run in a worker thread, one at a time per host,
    and a failed lookup is reused for ``auth_failure_ttl`` seconds of
    ``clock`` time before the host is looked up again.
    """

    def __init__(
        self,
        extra_gitlab_hosts: frozenset[str] = frozenset(),
        extra_github_hosts: frozenset[str] = frozenset(),
        request_timeout: float = 30.0,
        cache: object | None = None,
        mr_list_ttl: int = 60,
        diff_ttl: int = 300,
        *,
        max_parallel: int | None = None,
        auth_failure_ttl: float = AUTH_FAILURE_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        if max_parallel is not None and max_parallel < 1:
            raise ValueError("max_parallel must be positive")
        self._extra_gitlab_hosts = extra_gitlab_hosts
        self._extra_github_hosts = extra_github_hosts
        self._timeout = request_timeout
        self._cache = cache
        self._mr_list_ttl = mr_list_ttl
        self._diff_ttl = diff_ttl
        self._max_parallel = max_parallel
        self._auth_failure_ttl = auth_failure_ttl
        self._clock = clock
        self._clients: dict[str, ForgeClient] = {}
        self._hosts: dict[str, ForgeHost] = {}
        self._limiters: dict[str, HostRequestLimiter] = {}
        self._token_lookups: dict[str, asyncio.Task[str]] = {}
        self._auth_failures: dict[str, tuple[float, str]] = {}

    @property
    def max_parallel(self) -> int | None:
        """The per-host request bound, or ``None`` when requests are unbounded."""
        return self._max_parallel

    def get_host(self, hostname: str) -> ForgeHost | None:
        """Get or create a ForgeHost for the given hostname."""
        if hostname in self._hosts:
            return self._hosts[hostname]

        forge_type = self._detect_type(hostname)
        if forge_type is None:
            return None

        if forge_type == ForgeType.GITHUB:
            api_base = _github_api_base(hostname)
        else:
            api_base = _gitlab_api_base(hostname)

        host = ForgeHost(
            hostname=hostname,
            forge_type=forge_type,
            api_base=api_base,
        )
        self._hosts[hostname] = host
        return host

    async def get_client(self, hostname: str) -> ForgeClient:
        """Get or create an authenticated ForgeClient for the given host."""
        if hostname in self._clients:
            return self._clients[hostname]

        host = self.get_host(hostname)
        if host is None:
            raise AuthError(f"Unknown forge host: {hostname}")

        token = await self._lookup_token(host)
        # Another caller may have created the client while this one waited.
        existing = self._clients.get(hostname)
        if existing is not None:
            return existing

        http_client = create_client(
            host.api_base,
            token,
            self._timeout,
            refresh=partial(refresh_token, hostname, host.forge_type),
            limiter=self._limiter_for(hostname),
        )

        if host.forge_type == ForgeType.GITLAB:
            from tongs.forges.gitlab import GitLabClient

            client = GitLabClient(host, http_client)
        else:
            from tongs.forges.github import GitHubClient

            client = GitHubClient(host, http_client)

        if self._cache is not None:
            from tongs.cache.cached_client import CachedForgeClient

            client = CachedForgeClient(
                inner=client,
                cache=self._cache,
                hostname=hostname,
                mr_list_ttl=self._mr_list_ttl,
                diff_ttl=self._diff_ttl,
            )

        self._clients[hostname] = client
        return client

    def forget_auth_failures(self, hostname: str | None = None) -> None:
        """Drop remembered token lookup failures so the next access looks again.

        With ``hostname`` only that host is forgotten; otherwise every host is.
        A lookup still running for a forgotten host no longer records its
        result, and the next access starts a new one.
        """
        if hostname is None:
            self._auth_failures.clear()
            self._token_lookups.clear()
            return
        self._auth_failures.pop(hostname, None)
        self._token_lookups.pop(hostname, None)

    async def _lookup_token(self, host: ForgeHost) -> str:
        """Resolve the host's token off the event loop, sharing in-flight lookups.

        The lookup can run CLI tools or wait on a keyring, so it runs in a
        worker thread and never blocks lookups or requests for other hosts.
        Callers for the same host share one lookup. A cancelled caller leaves
        the lookup running for the others.
        """
        hostname = host.hostname
        failure = self._auth_failures.get(hostname)
        if failure is not None:
            failed_at, message = failure
            if self._clock() - failed_at < self._auth_failure_ttl:
                raise AuthError(message)
            del self._auth_failures[hostname]

        loop = asyncio.get_running_loop()
        lookup = self._token_lookups.get(hostname)
        if lookup is None or lookup.get_loop() is not loop:
            lookup = loop.create_task(
                asyncio.to_thread(resolve_token, hostname, host.forge_type)
            )
            self._token_lookups[hostname] = lookup
            lookup.add_done_callback(partial(self._finish_lookup, hostname))
        return await asyncio.shield(lookup)

    def _finish_lookup(self, hostname: str, lookup: asyncio.Task[str]) -> None:
        if self._token_lookups.get(hostname) is not lookup:
            # Forgotten while running: the result no longer counts.
            if not lookup.cancelled():
                lookup.exception()
            return
        del self._token_lookups[hostname]
        if lookup.cancelled():
            return
        error = lookup.exception()
        if isinstance(error, AuthError):
            self._auth_failures[hostname] = (self._clock(), str(error))

    def _limiter_for(self, hostname: str) -> HostRequestLimiter | None:
        if self._max_parallel is None:
            return None
        limiter = self._limiters.get(hostname)
        if limiter is None:
            limiter = HostRequestLimiter(self._max_parallel)
            self._limiters[hostname] = limiter
        return limiter

    def active_hostnames(self) -> list[str]:
        """Return the configured hostnames.

        These are github.com, gitlab.com and the extra GitLab and GitHub hosts
        from config.toml. Hosts seen only through local repository discovery
        are not included.
        """
        defaults = {"gitlab.com", "github.com"}
        all_hosts = (
            defaults | set(self._extra_gitlab_hosts) | set(self._extra_github_hosts)
        )
        return sorted(all_hosts)

    async def close_all(self) -> None:
        """Close all cached clients."""
        clients = list(self._clients.values())
        self._clients.clear()
        if not clients:
            return
        results = await asyncio.gather(
            *(client.close() for client in clients), return_exceptions=True
        )
        cancellations = [
            result for result in results if isinstance(result, asyncio.CancelledError)
        ]
        if cancellations:
            raise cancellations[0]
        failures = [result for result in results if isinstance(result, Exception)]
        if failures:
            raise ExceptionGroup("Failed to close forge clients", failures)

    def _detect_type(self, hostname: str) -> ForgeType | None:
        """Detect forge type for a configured hostname.

        Only exact matches are admitted: github.com, gitlab.com and the extra
        hosts from config. Any other hostname, including one that merely
        contains "github" or "gitlab", returns None so no client or token
        lookup can target it.
        """
        if hostname in {"github.com"} or hostname in self._extra_github_hosts:
            return ForgeType.GITHUB
        if hostname in {"gitlab.com"} or hostname in self._extra_gitlab_hosts:
            return ForgeType.GITLAB
        return None
