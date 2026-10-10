"""Tests for MCP server helpers."""

import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from tongs.config import Config
from tongs.errors import AuthError, NetworkError
from tongs.forges.models import CIStatus, Pipeline
from tongs.forges.registry import ForgeRegistry

mcp_available = True
try:
    from tongs.mcp import server
    from tongs.mcp.server import _parse_host_repo
except ImportError:
    mcp_available = False

pytestmark = pytest.mark.skipif(not mcp_available, reason="mcp not installed")

UNCONFIGURED_HOST = "evil-gitlab.example"


@pytest.fixture(autouse=True)
def configured_registry(monkeypatch):
    """Use a registry with one extra host per forge instead of the user's config."""
    registry = ForgeRegistry(
        extra_gitlab_hosts=frozenset({"git.lab.corp"}),
        extra_github_hosts=frozenset({"git.hub.corp"}),
    )
    monkeypatch.setattr(server, "_registry", registry)
    return registry


@pytest.fixture
def forbid_token_lookup():
    with patch(
        "tongs.forges.registry.resolve_token",
        side_effect=AssertionError("token lookup must not run"),
    ) as resolve:
        yield resolve


class TestParseHostRepo:
    def test_valid_github(self):
        """Standard github.com/owner/repo parses correctly."""
        host, repo = _parse_host_repo("github.com/owner/repo")
        assert host == "github.com"
        assert repo == "owner/repo"

    def test_nested_gitlab(self):
        """Nested GitLab group path is kept intact after the hostname."""
        host, repo = _parse_host_repo("gitlab.com/group/subgroup/repo")
        assert host == "gitlab.com"
        assert repo == "group/subgroup/repo"

    def test_configured_extra_hosts(self):
        """Hosts from config.toml are accepted for both forges."""
        assert _parse_host_repo("git.lab.corp/a/b/c") == ("git.lab.corp", "a/b/c")
        assert _parse_host_repo("git.hub.corp/a/b") == ("git.hub.corp", "a/b")

    def test_github_requires_exactly_owner_repo(self):
        """GitHub has no nested groups, so extra segments are rejected."""
        with pytest.raises(ValueError, match="GitHub host"):
            _parse_host_repo("github.com/owner/repo/extra")
        with pytest.raises(ValueError, match="GitHub host"):
            _parse_host_repo("git.hub.corp/owner/repo/extra")

    @pytest.mark.parametrize("repo_path", ["noslash", ""])
    def test_malformed_input_raises(self, repo_path):
        """Input without a host and path is rejected."""
        with pytest.raises(ValueError, match="repo_path must be"):
            _parse_host_repo(repo_path)

    def test_path_traversal_rejected(self):
        """Path traversal segments (..) are rejected by the regex."""
        with pytest.raises(ValueError, match="repo_path must be"):
            _parse_host_repo("../foo/bar")

    def test_non_ascii_segment_rejected(self):
        """Unicode word characters do not pass the ASCII-only path pattern."""
        with pytest.raises(ValueError, match="repo_path must be"):
            _parse_host_repo("github.com/оwner/repo")

    def test_unconfigured_host_rejected(self):
        """A host outside the configured set is rejected."""
        with pytest.raises(ValueError, match="not configured"):
            _parse_host_repo(f"{UNCONFIGURED_HOST}/owner/repo")


class TestToolsRejectUnconfiguredHosts:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("tool", "args"),
        [
            ("list_mrs", ()),
            ("get_mr", (1,)),
            ("get_mr_diff", (1,)),
            ("post_comment", (1, "hi")),
            ("approve_mr", (1,)),
            ("list_pipelines", (1,)),
        ],
    )
    async def test_rejects_before_token_lookup(
        self, forbid_token_lookup, configured_registry, tool, args
    ):
        """Every tool, including the write tools, rejects before credentials."""
        with (
            patch(
                "tongs.forges.registry.create_client",
                side_effect=AssertionError("no HTTP client may be built"),
            ),
            pytest.raises(ValueError),
        ):
            await getattr(server, tool)(f"{UNCONFIGURED_HOST}/owner/repo", *args)
        forbid_token_lookup.assert_not_called()
        assert configured_registry._clients == {}

    @pytest.mark.asyncio
    async def test_configured_extra_host_reaches_client(self, configured_registry):
        """A configured self-hosted GitLab still gets a client and a request."""
        with (
            patch("tongs.forges.registry.resolve_token", return_value="tok"),
            patch(
                "tongs.forges.gitlab.GitLabClient.list_mrs", return_value=[]
            ) as list_mrs,
        ):
            result = await server.list_mrs("git.lab.corp/group/sub/app")
        assert result == []
        list_mrs.assert_awaited_once()
        assert list_mrs.await_args.args[0] == "group/sub/app"
        await configured_registry.close_all()


TOOL_CALLS = [
    ("list_mrs", {}),
    ("get_mr", {"number": 1}),
    ("get_mr_diff", {"number": 1}),
    ("post_comment", {"number": 1, "body": "hi"}),
    ("approve_mr", {"number": 1}),
    ("list_pipelines", {"number": 1}),
]


class TestToolsRedactForgeErrors:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(("tool", "args"), TOOL_CALLS)
    async def test_forge_error_text_never_reaches_client(self, tool, args):
        """Every tool replaces forge error text with a fixed message."""
        secret = "https://user:zqSECRETzq@git.lab.corp token zqSECRETzq"
        with (
            patch(
                "tongs.forges.registry.resolve_token",
                side_effect=AuthError(secret),
            ),
            pytest.raises(server.ToolError) as excinfo,
        ):
            await getattr(server, tool)("git.lab.corp/group/app", **args)
        message = str(excinfo.value)
        assert "SECRET" not in message
        assert message == f"{tool} failed: Forge authentication is unavailable."
        assert excinfo.value.__cause__ is None
        assert excinfo.value.__suppress_context__ is True

    @pytest.mark.asyncio
    async def test_request_error_is_redacted(self, configured_registry):
        with (
            patch("tongs.forges.registry.resolve_token", return_value="tok"),
            patch(
                "tongs.forges.gitlab.GitLabClient.list_mrs",
                side_effect=NetworkError("connect to zqSECRETzq failed"),
            ),
            pytest.raises(server.ToolError) as excinfo,
        ):
            await server.list_mrs("git.lab.corp/group/app")
        assert "SECRET" not in str(excinfo.value)
        assert "could not be reached" in str(excinfo.value)
        await configured_registry.close_all()

    @pytest.mark.asyncio
    async def test_malformed_netrc_through_mcp_call(self, tmp_path):
        """A malformed .netrc reaches an MCP client without its contents."""
        netrc_file = tmp_path / ".netrc"
        netrc_file.write_text("machine git.lab.corp login x zqSECRETzq-value\n")
        netrc_file.chmod(0o600)
        with (
            patch("tongs.forges.auth.Path.home", return_value=tmp_path),
            patch("tongs.forges.auth._token_from_cli", return_value=None),
            patch("tongs.forges.auth._token_from_keyring", return_value=None),
            pytest.raises(server.ToolError) as excinfo,
        ):
            await server.mcp.call_tool(
                "list_mrs", {"repo_path": "git.lab.corp/group/app"}
            )
        message = str(excinfo.value)
        assert "SECRET" not in message
        assert "Forge authentication is unavailable" in message


@pytest.mark.asyncio
async def test_get_mr_diff_synthesizes_only_missing_headers(monkeypatch) -> None:
    client = AsyncMock()
    client.get_mr_diff.return_value = [
        {"old_path": "old.py", "new_path": "new.py", "diff": "@@ -1 +1 @@\n-old\n+new"},
        {
            "old_path": "same.py",
            "new_path": "same.py",
            "diff": "--- a/same.py\n+++ b/same.py\n@@ -1 +1 @@\n-a\n+b",
        },
    ]
    monkeypatch.setattr(
        server, "_client_for", AsyncMock(return_value=(client, "org/repo"))
    )
    result = await server.get_mr_diff("github.com/org/repo", 1)
    assert result.startswith("--- a/old.py\n+++ b/new.py\n@@")
    assert result.count("--- a/same.py") == 1


@pytest.mark.asyncio
async def test_list_pipelines_shortens_sha(monkeypatch) -> None:
    client = AsyncMock()
    client.list_mr_pipelines.return_value = [
        Pipeline(1, CIStatus.SUCCESS, "main", "1234567890abcdef", "https://ci")
    ]
    monkeypatch.setattr(
        server, "_client_for", AsyncMock(return_value=(client, "org/repo"))
    )
    result = await server.list_pipelines("github.com/org/repo", 1)
    assert result[0]["sha"] == "1234567"


def _pull(number: int) -> dict[str, object]:
    return {
        "number": number,
        "title": f"PR {number}",
        "state": "open",
        "user": {"login": "alice"},
        "head": {"ref": "feature", "sha": f"sha-{number}"},
        "base": {"ref": "main"},
        "created_at": "2026-09-01T00:00:00Z",
        "updated_at": "2026-09-02T00:00:00Z",
        "html_url": f"https://github.com/acme/repo/pull/{number}",
        "labels": [],
    }


@pytest.mark.asyncio
async def test_mcp_registry_bounds_requests_per_host(monkeypatch) -> None:
    """tongs-mcp builds its registry with max_parallel from config.toml."""
    active = 0
    peak = 0
    paths: list[str] = []

    async def handle_async_request(
        _transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        nonlocal active, peak
        paths.append(request.url.path)
        active += 1
        peak = max(peak, active)
        try:
            for _ in range(3):
                await asyncio.sleep(0)
            await asyncio.sleep(0.002)
            if request.url.path == "/repos/acme/repo/pulls":
                return httpx.Response(200, json=[_pull(n) for n in range(1, 51)])
            return httpx.Response(200, json={"check_runs": []})
        finally:
            active -= 1

    monkeypatch.setattr(
        httpx.AsyncHTTPTransport, "handle_async_request", handle_async_request
    )
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    monkeypatch.setattr(server, "_registry", None)
    monkeypatch.setattr(server, "load_config", lambda: Config(max_parallel=4))

    with patch("tongs.forges.registry.resolve_token", return_value="tok"):
        results = await asyncio.gather(
            server.list_mrs("github.com/acme/repo"),
            server.list_mrs("github.com/acme/repo"),
        )
    registry = server._registry
    assert registry is not None
    assert registry.max_parallel == 4
    await registry.close_all()

    assert [len(result) for result in results] == [50, 50]
    assert sum(path.endswith("/check-runs") for path in paths) == 100
    assert peak == 4


def _merged_pull(number: int) -> dict[str, object]:
    return {
        **_pull(number),
        "state": "closed",
        "merged_at": "2026-09-03T00:00:00Z",
    }


def _closed_pull(number: int) -> dict[str, object]:
    return {**_pull(number), "state": "closed", "merged_at": None}


@pytest.mark.asyncio
async def test_github_merged_filter_lists_merged_pulls(monkeypatch) -> None:
    """GitHub has no merged filter, so state=merged reads closed and keeps merged."""
    pull_params: list[dict[str, str]] = []

    async def handle_async_request(
        _transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        if request.url.path == "/repos/acme/repo/pulls":
            pull_params.append(dict(request.url.params))
            return httpx.Response(200, json=[_merged_pull(1), _closed_pull(2)])
        return httpx.Response(200, json={"check_runs": [], "total_count": 0})

    monkeypatch.setattr(
        httpx.AsyncHTTPTransport, "handle_async_request", handle_async_request
    )
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)

    with patch("tongs.forges.registry.resolve_token", return_value="tok"):
        merged = await server.list_mrs("github.com/acme/repo", state="merged")
        closed = await server.list_mrs("github.com/acme/repo", state="closed")
    await server._registry.close_all()

    assert [mr["number"] for mr in merged] == [1]
    assert [mr["number"] for mr in closed] == [1, 2]
    assert [params["state"] for params in pull_params] == ["closed", "closed"]


@pytest.mark.asyncio
async def test_gitlab_merged_filter_is_passed_through(configured_registry) -> None:
    with (
        patch("tongs.forges.registry.resolve_token", return_value="tok"),
        patch("tongs.forges.gitlab.GitLabClient.list_mrs", return_value=[]) as list_mrs,
    ):
        await server.list_mrs("git.lab.corp/group/app", state="merged")
    assert list_mrs.await_args.kwargs["state"] == "merged"
    await configured_registry.close_all()
