"""All Open tab warnings for per-repository failures (issue #293).

The All Open tab reads each repository on its own, so its warnings name the
repository that failed. A failure that belongs to the whole host, such as a
rejected sign-in, would otherwise repeat once per repository and is collapsed
into one entry for the host with a count.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_tui_session import (
    MockForgeClient,
    MockForgeRegistry,
    make_app,
    make_repo,
    make_summary,
    settle,
)
from tongs.errors import AuthError
from tongs.forges.models import ForgeHost
from tongs.scanner.repo import ForgeType
from tongs.services.errors import ServiceErrorCode
from tongs.services.models import HostFailure, RepositoryRef
from tongs.views.inbox import _failure_details
from tongs.widgets.mr_table import MRTable

AUTH_MESSAGE = "Forge authentication is unavailable."
NOT_FOUND_MESSAGE = "The requested forge resource was not found."


def _failure(
    hostname: str,
    code: ServiceErrorCode,
    message: str,
    project_path: str | None = None,
) -> HostFailure:
    repository = (
        RepositoryRef(hostname, project_path) if project_path is not None else None
    )
    return HostFailure(hostname, code, message, retryable=False, repository=repository)


@pytest.mark.asyncio
async def test_host_wide_auth_failure_is_one_line_with_a_count(
    tmp_path: Path,
) -> None:
    github = ForgeHost("github.com", ForgeType.GITHUB, "https://api.github.com")
    gitlab = ForgeHost("gitlab.com", ForgeType.GITLAB, "https://gitlab.com/api/v4")
    github_client = MockForgeClient(github, [make_summary(github)])
    gitlab_client = MockForgeClient(gitlab, [])
    gitlab_client.error = AuthError("private token detail")
    registry = MockForgeRegistry(
        {github.hostname: github_client, gitlab.hostname: gitlab_client}
    )
    repos = [make_repo(tmp_path)] + [
        make_repo(
            tmp_path,
            f"team/tool-{index}",
            hostname="gitlab.com",
            forge_type=ForgeType.GITLAB,
        )
        for index in range(3)
    ]
    app, _session, _cache = make_app(tmp_path, repos, registry)

    async with app.run_test(notifications=True) as pilot:
        await settle(app)
        app.clear_notifications()
        await pilot.press("3")
        await settle(app)
        table = app.screen.query_one("#all-open-table", MRTable)
        assert table.row_count == 1
        gitlab_reads = sorted(
            path for name, path in gitlab_client.calls if name == "all_open"
        )
        assert gitlab_reads == ["team/tool-0", "team/tool-1", "team/tool-2"]
        messages = [
            str(notice.message)
            for notice in app._notifications
            if str(notice.message).startswith("All Open")
        ]
        assert messages == [
            f"All Open skipped: gitlab.com: {AUTH_MESSAGE} (3 repositories)"
        ]
        assert "private token detail" not in messages[0]


def test_repository_specific_failures_each_name_their_repository() -> None:
    failures = [
        _failure("github.com", ServiceErrorCode.NOT_FOUND, NOT_FOUND_MESSAGE, "a/one"),
        _failure("github.com", ServiceErrorCode.NOT_FOUND, NOT_FOUND_MESSAGE, "a/two"),
    ]

    assert _failure_details(failures) == (
        f"github.com/a/one: {NOT_FOUND_MESSAGE}; github.com/a/two: {NOT_FOUND_MESSAGE}"
    )


def test_one_host_wide_failure_still_names_its_repository() -> None:
    failures = [
        _failure(
            "github.com", ServiceErrorCode.AUTHENTICATION_FAILED, AUTH_MESSAGE, "a/one"
        )
    ]

    assert _failure_details(failures) == f"github.com/a/one: {AUTH_MESSAGE}"


def test_host_wide_failures_group_per_host_and_code() -> None:
    rate = "The forge rate limit was reached."
    failures = [
        _failure("github.com", ServiceErrorCode.RATE_LIMITED, rate, "a/one"),
        _failure("gitlab.com", ServiceErrorCode.RATE_LIMITED, rate, "b/one"),
        _failure("github.com", ServiceErrorCode.NOT_FOUND, NOT_FOUND_MESSAGE, "a/two"),
        _failure("github.com", ServiceErrorCode.RATE_LIMITED, rate, "a/three"),
        _failure("gitlab.com", ServiceErrorCode.RATE_LIMITED, rate, "b/two"),
    ]

    assert _failure_details(failures).split("; ") == [
        f"github.com: {rate} (2 repositories)",
        f"gitlab.com: {rate} (2 repositories)",
        f"github.com/a/two: {NOT_FOUND_MESSAGE}",
    ]


def test_host_failure_without_a_repository_keeps_the_host_form() -> None:
    failures = [
        _failure("github.com", ServiceErrorCode.AUTHENTICATION_FAILED, AUTH_MESSAGE),
        _failure("gitlab.com", ServiceErrorCode.AUTHENTICATION_FAILED, AUTH_MESSAGE),
    ]

    assert _failure_details(failures) == (
        f"github.com: {AUTH_MESSAGE}; gitlab.com: {AUTH_MESSAGE}"
    )
