"""Tests for GitHub client."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest

import tongs.forges.github as github_module
from tongs.errors import (
    ConflictError,
    ForgeError,
    NotFoundError,
    PartialResultsError,
    ValidationError,
)
from tongs.forges.github import (
    GitHubClient,
    _aggregate_check_runs,
    _parse_ci_status,
)
from tongs.forges.models import (
    CIStatus,
    ForgeHost,
    ReviewDecision,
    SourceCleanupStatus,
)
from tongs.scanner.repo import ForgeType

_TEST_HOST = ForgeHost(
    hostname="github.com",
    forge_type=ForgeType.GITHUB,
    api_base="https://api.github.com",
)


def _make_github_client(handler) -> tuple[GitHubClient, httpx.AsyncClient]:
    """Create a GitHubClient backed by a MockTransport."""
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(
        transport=transport,
        base_url=_TEST_HOST.api_base,
    )
    return GitHubClient(_TEST_HOST, http), http


def _pr_api_json(overrides: dict | None = None) -> dict:
    """Return a realistic GitHub PR API response dict."""
    data = {
        "number": 10,
        "title": "Add feature",
        "state": "open",
        "draft": False,
        "html_url": "https://github.com/acme/repo/pull/10",
        "created_at": "2026-07-01T09:00:00Z",
        "updated_at": "2026-07-02T14:30:00Z",
        "comments": 2,
        "review_comments": 1,
        "labels": [],
        "user": {"login": "alice", "name": "Alice"},
        "head": {"ref": "feature-branch", "sha": "abc123def456"},
        "base": {
            "ref": "main",
            "sha": "base123def456",
            "repo": {"full_name": "acme/repo"},
        },
        "mergeable_state": "clean",
    }
    if overrides:
        data.update(overrides)
    return data


def _review_comment_json(overrides: dict | None = None) -> dict:
    """Return a GitHub review comment response."""
    data = {
        "id": 999,
        "body": "Looks good",
        "user": {"login": "bob", "name": "Bob"},
        "created_at": "2026-07-02T10:00:00Z",
        "path": "src/main.py",
        "line": 42,
        "side": "RIGHT",
    }
    if overrides:
        data.update(overrides)
    return data


def _merge_pr_json(**overrides: object) -> dict:
    data = _pr_api_json(
        {
            "head": {
                "ref": "feature/branch",
                "sha": "captured-head",
                "repo": {"full_name": "acme/repo"},
            },
            "base": {
                "ref": "main",
                "sha": "base123def456",
                "repo": {"full_name": "acme/repo", "default_branch": "main"},
            },
        }
    )
    data.update(overrides)
    return data


def _job_json(job_id: int) -> dict:
    return {
        "id": job_id,
        "name": f"job-{job_id}",
        "workflow_name": "verify",
        "status": "completed",
        "conclusion": "success",
    }


class TestPipelineJobPagination:
    @pytest.mark.asyncio
    async def test_collects_all_envelope_pages(self) -> None:
        seen_pages: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            page = request.url.params["page"]
            seen_pages.append(page)
            jobs = (
                [_job_json(index) for index in range(1, 101)]
                if page == "1"
                else [_job_json(202)]
            )
            return httpx.Response(200, json={"total_count": 101, "jobs": jobs})

        client, http = _make_github_client(handler)
        async with http:
            jobs = await client.get_pipeline_jobs("acme/repo", 77)

        assert seen_pages == ["1", "2"]
        assert [job.id for job in jobs][-2:] == [100, 202]

    @pytest.mark.asyncio
    async def test_rejects_incomplete_envelope(self) -> None:
        client, http = _make_github_client(
            lambda _request: httpx.Response(200, json={"total_count": 2, "jobs": []})
        )
        async with http:
            with pytest.raises(ForgeError, match="incomplete"):
                await client.get_pipeline_jobs("acme/repo", 77)


_GHE_HOST = ForgeHost(
    hostname="github.corp.example.com",
    forge_type=ForgeType.GITHUB,
    api_base="https://github.corp.example.com/api/v3",
)


class TestGraphQLProperties:
    def test_graphql_url_github_com(self):
        """github.com uses api.github.com/graphql."""
        transport = httpx.MockTransport(lambda _: httpx.Response(200))
        http = httpx.AsyncClient(transport=transport, base_url=_TEST_HOST.api_base)
        client = GitHubClient(_TEST_HOST, http)
        assert client._graphql_url == "https://api.github.com/graphql"

    def test_graphql_url_ghe(self):
        """GHE hostname uses hostname/api/graphql."""
        transport = httpx.MockTransport(lambda _: httpx.Response(200))
        http = httpx.AsyncClient(transport=transport, base_url=_GHE_HOST.api_base)
        client = GitHubClient(_GHE_HOST, http)
        assert client._graphql_url == "https://github.corp.example.com/api/graphql"

    def test_supports_thread_resolution_true(self):
        """GitHubClient advertises thread resolution support."""
        transport = httpx.MockTransport(lambda _: httpx.Response(200))
        http = httpx.AsyncClient(transport=transport, base_url=_TEST_HOST.api_base)
        client = GitHubClient(_TEST_HOST, http)
        assert client.supports_thread_resolution is True


class TestRevisionMetadata:
    def test_detail_captures_head_and_base_sha(self):
        client, _ = _make_github_client(lambda _: httpx.Response(200))
        detail = client._parse_pr_detail(_pr_api_json(), "acme/repo")
        assert detail.head_sha == "abc123def456"
        assert detail.base_sha == "base123def456"
        assert detail.start_sha is None


class TestChangesOverflow:
    """GitHub's pull request files API lists at most 3,000 files."""

    @pytest.mark.parametrize(
        ("changed_files", "count", "overflow"),
        [
            (3001, 3001, True),
            (3000, 3000, False),
            (12, 12, False),
        ],
    )
    def test_detail_flags_more_files_than_the_api_lists(
        self, changed_files: int, count: int, overflow: bool
    ):
        client, _ = _make_github_client(lambda _: httpx.Response(200))
        data = _pr_api_json({"changed_files": changed_files})
        detail = client._parse_pr_detail(data, "acme/repo")
        assert detail.changes_count == count
        assert detail.changes_overflow is overflow

    @pytest.mark.parametrize("changed_files", [None, True, "5000"])
    def test_detail_without_an_integer_count_does_not_overflow(
        self, changed_files: object
    ):
        client, _ = _make_github_client(lambda _: httpx.Response(200))
        data = _pr_api_json({"changed_files": changed_files})
        detail = client._parse_pr_detail(data, "acme/repo")
        assert detail.changes_overflow is False

    def test_missing_changed_files_does_not_overflow(self):
        client, _ = _make_github_client(lambda _: httpx.Response(200))
        detail = client._parse_pr_detail(_pr_api_json(), "acme/repo")
        assert detail.changes_count == 0
        assert detail.changes_overflow is False


class TestCreateInlineComment:
    @pytest.mark.asyncio
    async def test_single_line_no_start_line_or_start_side(self):
        """Single-line comment payload must NOT include start_line/start_side."""
        requests_made = []

        def handler(req: httpx.Request) -> httpx.Response:
            requests_made.append(req)
            if req.method == "GET":
                return httpx.Response(200, json=_pr_api_json())
            return httpx.Response(200, json=_review_comment_json())

        client, http = _make_github_client(handler)
        async with http:
            await client.create_inline_comment(
                repo_path="acme/repo",
                number=10,
                file_path="src/main.py",
                line=42,
                side="RIGHT",
                body="Fix this",
            )

        post_req = next(r for r in requests_made if r.method == "POST")
        payload = json.loads(post_req.content)
        assert payload["body"] == "Fix this"
        assert payload["path"] == "src/main.py"
        assert payload["line"] == 42
        assert payload["side"] == "RIGHT"
        assert payload["commit_id"] == "abc123def456"
        assert "start_line" not in payload
        assert "start_side" not in payload

    @pytest.mark.asyncio
    async def test_multi_line_includes_start_line_and_start_side(self):
        """Multi-line comment payload must include start_line and start_side."""
        requests_made = []

        def handler(req: httpx.Request) -> httpx.Response:
            requests_made.append(req)
            if req.method == "GET":
                return httpx.Response(200, json=_pr_api_json())
            return httpx.Response(200, json=_review_comment_json())

        client, http = _make_github_client(handler)
        async with http:
            await client.create_inline_comment(
                repo_path="acme/repo",
                number=10,
                file_path="src/main.py",
                line=50,
                side="RIGHT",
                body="Refactor this block",
                start_line=45,
                start_side="LEFT",
            )

        post_req = next(r for r in requests_made if r.method == "POST")
        payload = json.loads(post_req.content)
        assert payload["start_line"] == 45
        assert payload["start_side"] == "LEFT"
        assert payload["line"] == 50
        assert payload["side"] == "RIGHT"

    @pytest.mark.asyncio
    async def test_start_side_defaults_to_side_when_none(self):
        """When start_line is set but start_side is None, start_side defaults to side."""
        requests_made = []

        def handler(req: httpx.Request) -> httpx.Response:
            requests_made.append(req)
            if req.method == "GET":
                return httpx.Response(200, json=_pr_api_json())
            return httpx.Response(200, json=_review_comment_json())

        client, http = _make_github_client(handler)
        async with http:
            await client.create_inline_comment(
                repo_path="acme/repo",
                number=10,
                file_path="src/main.py",
                line=50,
                side="RIGHT",
                body="Needs work",
                start_line=45,
                start_side=None,
            )

        post_req = next(r for r in requests_made if r.method == "POST")
        payload = json.loads(post_req.content)
        assert payload["start_line"] == 45
        assert payload["start_side"] == "RIGHT"

    @pytest.mark.asyncio
    async def test_explicit_head_never_refetches_latest_revision(self):
        requests_made = []

        def handler(req: httpx.Request) -> httpx.Response:
            requests_made.append(req)
            assert req.method == "POST"
            return httpx.Response(200, json=_review_comment_json())

        client, http = _make_github_client(handler)
        async with http:
            await client.create_inline_comment(
                "acme/repo",
                10,
                "new.py",
                42,
                "RIGHT",
                "body",
                head_sha="captured-head",
                old_path="old.py",
                new_path="new.py",
            )
        assert [request.method for request in requests_made] == ["POST"]
        assert json.loads(requests_made[0].content)["commit_id"] == "captured-head"


class TestReviewMutationRoutes:
    @pytest.mark.asyncio
    async def test_reply_includes_pull_number_and_top_level_comment(self):
        seen = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append(req)
            return httpx.Response(200, json=_review_comment_json({"id": 1001}))

        client, http = _make_github_client(handler)
        async with http:
            result = await client.reply_to_discussion(
                "acme/repo", 10, "thread", "reply", root_comment_id="999"
            )
        assert seen[0].url.path.endswith("/pulls/10/comments/999/replies")
        assert result.comment_id == "1001"

    @pytest.mark.asyncio
    async def test_discussions_group_replies_under_their_root(self):
        comments = [
            _review_comment_json({"id": 999}),
            _review_comment_json({"id": 1000, "in_reply_to_id": 999}),
        ]
        client, http = _make_github_client(lambda _: httpx.Response(200, json=comments))
        async with http:
            discussions = await client.get_mr_discussions("acme/repo", 10)
        assert len(discussions) == 1
        assert discussions[0].root_comment.replies[0].id == "1000"

    @pytest.mark.asyncio
    async def test_review_payload_binds_top_level_commit_id(self):
        seen = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append(req)
            return httpx.Response(200, json={"id": 77})

        client, http = _make_github_client(handler)
        async with http:
            await client.submit_review(
                "acme/repo",
                10,
                ReviewDecision.APPROVED,
                "",
                head_sha="captured-head",
            )
        assert json.loads(seen[0].content)["commit_id"] == "captured-head"

    @pytest.mark.asyncio
    async def test_approve_maps_self_approval_422(self):
        client, http = _make_github_client(
            lambda _: httpx.Response(
                422, json={"message": "Can not approve your own pull request"}
            )
        )
        async with http:
            with pytest.raises(
                ValidationError, match="GitHub does not allow self-approving PRs"
            ):
                await client.approve_mr("acme/repo", 10)

    @pytest.mark.asyncio
    async def test_approve_preserves_other_validation_messages(self):
        client, http = _make_github_client(
            lambda _: httpx.Response(422, json={"message": "commit_id is stale"})
        )
        async with http:
            with pytest.raises(ValidationError, match="commit_id is stale"):
                await client.approve_mr("acme/repo", 10)

    @pytest.mark.asyncio
    async def test_thread_lookup_paginates_roots_and_reply_comments(self, monkeypatch):
        client, http = _make_github_client(lambda _: httpx.Response(200))
        pages = [
            {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": [],
                            "pageInfo": {"hasNextPage": True, "endCursor": "roots-2"},
                        }
                    }
                }
            },
            {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": [
                                {
                                    "id": "thread-node",
                                    "comments": {
                                        "nodes": [{"databaseId": 1}],
                                        "pageInfo": {
                                            "hasNextPage": True,
                                            "endCursor": "comments-2",
                                        },
                                    },
                                }
                            ],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            },
            {
                "node": {
                    "comments": {
                        "nodes": [{"databaseId": 999}],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            },
        ]
        graphql = AsyncMock(side_effect=pages)
        monkeypatch.setattr(client, "_graphql", graphql)
        assert (
            await client._find_thread_node_id("acme", "repo", 10, 999) == "thread-node"
        )
        assert graphql.await_count == 3
        await http.aclose()


class TestGitHubLifecycleActions:
    @pytest.mark.asyncio
    async def test_merge_binds_head_and_confirms_same_repo_cleanup(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.method == "GET" and "/pulls/10" in request.url.path:
                return httpx.Response(200, json=_merge_pr_json())
            if request.method == "PUT":
                return httpx.Response(200, json={"merged": True, "sha": "merge-sha"})
            if request.method == "GET":
                return httpx.Response(200, json={"object": {"sha": "captured-head"}})
            assert request.method == "DELETE"
            return httpx.Response(204)

        client, http = _make_github_client(handler)
        async with http:
            result = await client.merge_mr(
                "acme/repo",
                10,
                True,
                True,
                head_sha="captured-head",
                expected_source_repository="acme/repo",
                expected_source_branch="feature/branch",
                expected_target_branch="main",
            )

        assert result.merge_sha == "merge-sha"
        assert result.source_cleanup is SourceCleanupStatus.CONFIRMED
        assert [item.method for item in requests] == ["GET", "PUT", "GET", "DELETE"]
        assert json.loads(requests[1].content) == {
            "merge_method": "squash",
            "sha": "captured-head",
        }
        assert requests[2].url.path.endswith("/git/ref/heads/feature/branch")
        assert requests[3].url.path.endswith("/git/refs/heads/feature/branch")

    @pytest.mark.asyncio
    async def test_http_success_merged_false_never_deletes_branch(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.method == "GET":
                return httpx.Response(200, json=_merge_pr_json())
            return httpx.Response(
                200, json={"merged": False, "message": "checks failed"}
            )

        client, http = _make_github_client(handler)
        async with http:
            with pytest.raises(ConflictError):
                await client.merge_mr("acme/repo", 10, head_sha="captured-head")

        assert [item.method for item in requests] == ["GET", "PUT"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("unsafe", ["fork", "default", "target"])
    async def test_cleanup_guard_never_deletes_unsafe_source(self, unsafe: str) -> None:
        requests: list[httpx.Request] = []
        data = _merge_pr_json()
        if unsafe == "fork":
            data["head"]["repo"]["full_name"] = "someone/fork"
        elif unsafe == "default":
            data["head"]["ref"] = "main"
            data["base"]["ref"] = "release"
        else:
            data["head"]["ref"] = "release"
            data["base"]["ref"] = "release"

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.method == "GET":
                return httpx.Response(200, json=data)
            return httpx.Response(200, json={"merged": True, "sha": "merge-sha"})

        client, http = _make_github_client(handler)
        async with http:
            result = await client.merge_mr("acme/repo", 10)

        assert result.source_cleanup is SourceCleanupStatus.REJECTED
        assert [item.method for item in requests] == ["GET", "PUT"]

    @pytest.mark.asyncio
    async def test_cleanup_failure_preserves_confirmed_merge(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.method == "GET" and "/pulls/10" in request.url.path:
                return httpx.Response(200, json=_merge_pr_json())
            if request.method == "PUT":
                return httpx.Response(200, json={"merged": True, "sha": "merge-sha"})
            return httpx.Response(403, json={"message": "denied"})

        client, http = _make_github_client(handler)
        async with http:
            result = await client.merge_mr("acme/repo", 10)

        assert result.merge_sha == "merge-sha"
        assert result.source_cleanup is SourceCleanupStatus.UNKNOWN
        assert [item.method for item in requests] == ["GET", "PUT", "GET"]

    @pytest.mark.asyncio
    async def test_cleanup_cancellation_preserves_confirmed_merge(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cleanup_entered = asyncio.Event()
        calls = 0

        async def fake_request(*args: object, **kwargs: object) -> dict:
            nonlocal calls
            calls += 1
            if calls == 1:
                return _merge_pr_json()
            if calls == 2:
                return {"merged": True, "sha": "merge-sha"}
            cleanup_entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        monkeypatch.setattr(github_module, "request", fake_request)
        client, http = _make_github_client(lambda _: httpx.Response(500))
        task = asyncio.create_task(client.merge_mr("acme/repo", 10))
        await cleanup_entered.wait()
        task.cancel()

        result = await task

        assert result.merge_sha == "merge-sha"
        assert result.source_cleanup is SourceCleanupStatus.UNKNOWN
        assert task.cancelling() == 1
        await http.aclose()

    @pytest.mark.asyncio
    async def test_changed_branch_sha_rejects_cleanup_without_delete(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.method == "GET" and "/pulls/10" in request.url.path:
                return httpx.Response(200, json=_merge_pr_json())
            if request.method == "PUT":
                return httpx.Response(200, json={"merged": True, "sha": "merge-sha"})
            return httpx.Response(200, json={"object": {"sha": "changed"}})

        client, http = _make_github_client(handler)
        async with http:
            result = await client.merge_mr("acme/repo", 10)

        assert result.source_cleanup is SourceCleanupStatus.REJECTED
        assert [item.method for item in requests] == ["GET", "PUT", "GET"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("identity", [True, 1.0, 0, -1, "1"])
    async def test_state_action_rejects_malformed_native_identity(
        self, identity: object
    ) -> None:
        client, http = _make_github_client(
            lambda _: httpx.Response(
                200, json={"id": 5, "number": identity, "state": "closed"}
            )
        )
        async with http:
            with pytest.raises(ValueError, match="state response"):
                await client.close_mr("acme/repo", 1)

    @pytest.mark.asyncio
    async def test_close_and_reopen_require_exact_native_state(self) -> None:
        responses = iter(
            [
                {"id": 55, "number": 10, "state": "closed"},
                {"id": 55, "number": 10, "state": "open"},
                {"id": 55, "number": 10, "state": "open"},
            ]
        )
        client, http = _make_github_client(
            lambda _: httpx.Response(200, json=next(responses))
        )
        async with http:
            closed = await client.close_mr("acme/repo", 10)
            reopened = await client.reopen_mr("acme/repo", 10)
            with pytest.raises(ValueError, match="state response"):
                await client.close_mr("acme/repo", 10)

        assert closed.remote_id == "55"
        assert reopened.remote_id == "55"


def _run(conclusion: str | None, status: str = "completed") -> dict:
    return {"status": status, "conclusion": conclusion}


class TestCheckRunAggregation:
    @pytest.mark.parametrize(
        ("runs", "expected"),
        [
            ([_run("success"), _run("skipped")], CIStatus.SUCCESS),
            ([_run("neutral")], CIStatus.SUCCESS),
            ([_run("success"), _run("neutral"), _run("skipped")], CIStatus.SUCCESS),
            ([_run("skipped"), _run("skipped")], CIStatus.SKIPPED),
            ([_run("startup_failure")], CIStatus.FAILED),
            ([_run("success"), _run("timed_out")], CIStatus.FAILED),
            ([_run("success"), _run("stale")], CIStatus.PENDING),
            ([_run("success"), _run("action_required")], CIStatus.PENDING),
            ([_run("success"), _run(None, "queued")], CIStatus.PENDING),
            ([_run("success"), _run(None, "in_progress")], CIStatus.RUNNING),
            ([_run("failure"), _run(None, "in_progress")], CIStatus.FAILED),
            ([_run("success"), _run("cancelled")], CIStatus.CANCELED),
            ([_run("success"), _run("something_new")], CIStatus.UNKNOWN),
            ([], CIStatus.UNKNOWN),
        ],
    )
    def test_aggregate(self, runs: list[dict], expected: CIStatus) -> None:
        assert _aggregate_check_runs(runs) == expected

    @pytest.mark.parametrize(
        ("status", "conclusion", "expected"),
        [
            ("completed", "success", CIStatus.SUCCESS),
            ("completed", "skipped", CIStatus.SKIPPED),
            ("completed", "neutral", CIStatus.UNKNOWN),
            ("completed", "startup_failure", CIStatus.UNKNOWN),
            ("completed", "stale", CIStatus.UNKNOWN),
            ("completed", "cancelled", CIStatus.CANCELED),
            ("in_progress", None, CIStatus.RUNNING),
            ("queued", None, CIStatus.PENDING),
        ],
    )
    def test_pipeline_and_job_rows_keep_their_mapping(
        self, status: str, conclusion: str | None, expected: CIStatus
    ) -> None:
        assert _parse_ci_status(status, conclusion) == expected
        client = GitHubClient(_TEST_HOST, httpx.AsyncClient())
        run = client._parse_workflow_run(
            {"id": 1, "status": status, "conclusion": conclusion}
        )
        job = client._parse_job({"id": 2, "status": status, "conclusion": conclusion})
        assert (run.status, job.status) == (expected, expected)

    @pytest.mark.asyncio
    async def test_pages_check_runs_up_to_total_count(self) -> None:
        pages: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/repos/acme/repo/commits/abc/check-runs"
            page = int(request.url.params["page"])
            pages.append(str(page))
            runs = [_run("success") for _ in range(100 if page == 1 else 50)]
            if page == 2:
                # Run 120 fails; it is only visible on the second page.
                runs[19] = _run("failure")
            return httpx.Response(200, json={"total_count": 150, "check_runs": runs})

        client, http = _make_github_client(handler)
        async with http:
            status = await client._fetch_ci_status("acme", "repo", "abc")

        assert pages == ["1", "2"]
        assert status == CIStatus.FAILED

    @pytest.mark.asyncio
    async def test_success_over_every_page(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            page = int(request.url.params["page"])
            runs = [_run("success" if page == 1 else "skipped")] * (
                100 if page == 1 else 50
            )
            return httpx.Response(200, json={"total_count": 150, "check_runs": runs})

        client, http = _make_github_client(handler)
        async with http:
            status = await client._fetch_ci_status("acme", "repo", "abc")

        assert status == CIStatus.SUCCESS

    @pytest.mark.asyncio
    async def test_runs_beyond_the_page_limit_never_claim_success(self) -> None:
        pages: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            pages.append(request.url.params["page"])
            runs = [_run("success")] * 100
            return httpx.Response(200, json={"total_count": 5000, "check_runs": runs})

        client, http = _make_github_client(handler)
        async with http:
            status = await client._fetch_ci_status("acme", "repo", "abc")

        assert len(pages) == github_module._CHECK_RUNS_MAX_PAGES
        assert status == CIStatus.UNKNOWN

    @pytest.mark.asyncio
    async def test_response_without_total_count_reads_one_page(self) -> None:
        pages: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            pages.append(request.url.params["page"])
            return httpx.Response(200, json={"check_runs": [_run("success")]})

        client, http = _make_github_client(handler)
        async with http:
            status = await client._fetch_ci_status("acme", "repo", "abc")

        assert pages == ["1"]
        assert status == CIStatus.SUCCESS


def _search_item(number: int) -> dict:
    return {
        "number": number,
        "repository_url": "https://api.github.com/repos/acme/repo",
    }


def _search_handler(
    *,
    total_count: int,
    search_pages: list[str],
    failing_pr: int | None = None,
    failing_search_page: int | None = None,
    incomplete: bool = False,
):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/search/issues":
            page = int(request.url.params["page"])
            search_pages.append(str(page))
            if page == failing_search_page:
                return httpx.Response(502, json={"message": "bad gateway"})
            per_page = int(request.url.params["per_page"])
            start = (page - 1) * per_page
            numbers = range(start + 1, min(start + per_page, total_count) + 1)
            return httpx.Response(
                200,
                json={
                    "total_count": total_count,
                    "incomplete_results": incomplete,
                    "items": [_search_item(number) for number in numbers],
                },
            )
        number = int(request.url.path.rsplit("/", 1)[1])
        if number == failing_pr:
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(200, json=_pr_api_json({"number": number}))

    return handler


class TestSearchPaging:
    @pytest.mark.asyncio
    async def test_follows_search_pages_past_the_first_hundred(self) -> None:
        search_pages: list[str] = []
        client, http = _make_github_client(
            _search_handler(total_count=150, search_pages=search_pages)
        )
        async with http:
            results = await client.list_my_reviews()

        assert search_pages == ["1", "2"]
        assert [mr.number for mr in results] == list(range(1, 151))

    @pytest.mark.asyncio
    async def test_failed_pull_request_fetch_keeps_the_others(self) -> None:
        search_pages: list[str] = []
        client, http = _make_github_client(
            _search_handler(total_count=3, search_pages=search_pages, failing_pr=2)
        )
        async with http:
            with pytest.raises(PartialResultsError) as caught:
                await client.list_my_mrs()

        assert [mr.number for mr in caught.value.items] == [1, 3]
        assert isinstance(caught.value.cause, NotFoundError)
        assert "1 of 3" in str(caught.value)

    @pytest.mark.asyncio
    async def test_result_cap_reports_truncation(self, monkeypatch) -> None:
        monkeypatch.setattr(github_module, "_SEARCH_RESULT_CAP", 200)
        search_pages: list[str] = []
        client, http = _make_github_client(
            _search_handler(total_count=450, search_pages=search_pages)
        )
        async with http:
            with pytest.raises(PartialResultsError) as caught:
                await client.list_my_reviews()

        assert search_pages == ["1", "2"]
        assert len(caught.value.items) == 200
        assert "450" in str(caught.value.cause)

    @pytest.mark.asyncio
    async def test_incomplete_search_reports_truncation(self) -> None:
        search_pages: list[str] = []
        client, http = _make_github_client(
            _search_handler(total_count=2, search_pages=search_pages, incomplete=True)
        )
        async with http:
            with pytest.raises(PartialResultsError) as caught:
                await client.list_my_reviews()

        assert [mr.number for mr in caught.value.items] == [1, 2]
        assert "timed out" in str(caught.value.cause)

    @pytest.mark.asyncio
    async def test_failed_later_search_page_keeps_earlier_results(self) -> None:
        search_pages: list[str] = []
        client, http = _make_github_client(
            _search_handler(
                total_count=150, search_pages=search_pages, failing_search_page=2
            )
        )
        async with http:
            with pytest.raises(PartialResultsError) as caught:
                await client.list_my_reviews()

        assert search_pages == ["1", "2"]
        assert len(caught.value.items) == 100
        assert isinstance(caught.value.cause, ForgeError)

    @pytest.mark.asyncio
    async def test_failed_first_search_page_raises_its_error(self) -> None:
        search_pages: list[str] = []
        client, http = _make_github_client(
            _search_handler(
                total_count=5, search_pages=search_pages, failing_search_page=1
            )
        )
        async with http:
            with pytest.raises(ForgeError) as caught:
                await client.list_my_reviews()

        assert not isinstance(caught.value, PartialResultsError)
        assert search_pages == ["1"]
