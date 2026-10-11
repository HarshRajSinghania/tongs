"""Expanding diff context end to end, through the real session and forge clients.

Every forge request goes to an ``httpx.MockTransport`` that serves one review
with a two-hunk file and counts the file reads, so the tests check the request
cost rules as well as the line numbers: opening a diff reads no file, the
first ``e`` or ``F`` reads the new file at the review's head once, and nothing
after that reads it again.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import httpx
import pytest

from tests.test_diff.test_context import FIXTURES
from tests.test_tui_mr_services import _settle
from tests.test_tui_session import (
    MockForgeRegistry,
    make_app,
    make_repo,
    make_summary,
)
from tongs.app import TongsApp
from tongs.cache.cached_client import CachedForgeClient
from tongs.cache.store import CacheStore
from tongs.diff.models import DiffLine, LineType
from tongs.forges.github import GitHubClient
from tongs.forges.gitlab import GitLabClient
from tongs.forges.models import ForgeHost, MRSummary
from tongs.scanner.repo import ForgeType
from tongs.views.mr_detail import MRDetailScreen
from tongs.widgets.diff_panel import (
    FULL_FILE_LABEL,
    READ_ONLY_LINE_MESSAGE,
    DiffOptionList,
    DiffPanel,
)
from tongs.widgets.split_diff import DiffViewMode, SplitDiffColumn, SplitDiffView

PROJECT = "acme/widgets"
PATH = "src/pkg/widget notes.txt"
HEAD = "c0ffee" + "0" * 34
BASE = "ba5e" + "0" * 36
CREATED = "2026-10-01T12:00:00Z"
OLD_TEXT, NEW_TEXT, HUNKS = FIXTURES["two_hunks"]
OLD_LINES = OLD_TEXT.split("\n")[:-1]
NEW_LINES = NEW_TEXT.split("\n")[:-1]

GITHUB = ForgeHost("github.com", ForgeType.GITHUB, "https://api.github.com")
GITLAB = ForgeHost("gitlab.com", ForgeType.GITLAB, "https://gitlab.com/api/v4")


@dataclass
class _Forge:
    """What the mocked forge served: every request path and the file reads."""

    paths: list[str] = field(default_factory=list)
    file_reads: list[str] = field(default_factory=list)


def _json(payload: object) -> httpx.Response:
    return httpx.Response(200, content=json.dumps(payload).encode())


def _github_handler(forge: _Forge) -> Callable[[httpx.Request], httpx.Response]:
    pull = f"/repos/{PROJECT}/pulls/7"
    contents = f"/repos/{PROJECT}/contents/src/pkg/widget%20notes.txt"

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        forge.paths.append(path)
        if request.url.raw_path.decode().split("?")[0] == contents:
            assert request.url.params["ref"] == HEAD
            assert request.headers["Accept"] == "application/vnd.github.raw"
            forge.file_reads.append(path)
            return httpx.Response(200, content=NEW_TEXT.encode())
        if path == pull:
            return _json(
                {
                    "number": 7,
                    "title": "Rework the widget notes",
                    "state": "open",
                    "draft": False,
                    "user": {"login": "alice"},
                    "head": {"sha": HEAD, "ref": "feature"},
                    "base": {"sha": BASE, "ref": "main"},
                    "html_url": f"https://github.com/{PROJECT}/pull/7",
                    "created_at": CREATED,
                    "updated_at": CREATED,
                    "body": "",
                    "changed_files": 1,
                }
            )
        if path == f"{pull}/files":
            return _json(
                [
                    {
                        "filename": PATH,
                        "status": "modified",
                        "patch": HUNKS.rstrip("\n"),
                        "additions": 2,
                        "deletions": 2,
                        "changes": 4,
                    }
                ]
            )
        if path == "/graphql":
            return _json({"data": {}})
        return _json([])

    return handler


def _gitlab_handler(forge: _Forge) -> Callable[[httpx.Request], httpx.Response]:
    project = "acme%2Fwidgets"
    mr = f"/api/v4/projects/{project}/merge_requests/7"
    raw = f"/api/v4/projects/{project}/repository/files/src%2Fpkg%2Fwidget%20notes.txt/raw"

    def handler(request: httpx.Request) -> httpx.Response:
        raw_path = request.url.raw_path.decode().split("?")[0]
        forge.paths.append(raw_path)
        if raw_path == raw:
            assert request.url.params["ref"] == HEAD
            forge.file_reads.append(raw_path)
            return httpx.Response(200, content=NEW_TEXT.encode())
        if raw_path == mr:
            return _json(
                {
                    "iid": 7,
                    "title": "Rework the widget notes",
                    "state": "opened",
                    "draft": False,
                    "author": {"username": "alice"},
                    "source_branch": "feature",
                    "target_branch": "main",
                    "web_url": f"https://gitlab.com/{PROJECT}/-/merge_requests/7",
                    "created_at": CREATED,
                    "updated_at": CREATED,
                    "description": "",
                    "changes_count": "1",
                    "diff_refs": {
                        "base_sha": BASE,
                        "head_sha": HEAD,
                        "start_sha": BASE,
                    },
                }
            )
        if raw_path == f"{mr}/diffs":
            return _json(
                [
                    {
                        "old_path": PATH,
                        "new_path": PATH,
                        "diff": HUNKS,
                        "new_file": False,
                        "renamed_file": False,
                        "deleted_file": False,
                        "a_mode": "100644",
                        "b_mode": "100644",
                    }
                ]
            )
        if raw_path == f"{mr}/approvals":
            return _json({"approved_by": []})
        return _json([])

    return handler


class _ListingClient:
    """Serve the inbox listing locally; every review read goes over HTTP.

    The inbox only has to list the one review so the screen can open it. The
    detail, the diff and the file reads are the real client's requests.
    """

    def __init__(self, client: GitHubClient | GitLabClient, summary: MRSummary):
        self._client = client
        self._summary = summary

    async def list_my_reviews(self) -> list[MRSummary]:
        return [self._summary]

    async def list_my_mrs(self) -> list[MRSummary]:
        return []

    def __getattr__(self, name: str) -> object:
        return getattr(self._client, name)


async def _build(
    tmp_path: Path, host: ForgeHost
) -> tuple[TongsApp, _Forge, httpx.AsyncClient]:
    forge = _Forge()
    if host.forge_type is ForgeType.GITHUB:
        handler = _github_handler(forge)
    else:
        handler = _gitlab_handler(forge)
    http = httpx.AsyncClient(
        base_url=host.api_base,
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    )
    real: GitHubClient | GitLabClient = (
        GitHubClient(host, http)
        if host.forge_type is ForgeType.GITHUB
        else GitLabClient(host, http)
    )
    cache = CacheStore(tmp_path / "cache" / "cache.db")
    cached = CachedForgeClient(real, cache, hostname=host.hostname)
    summary = make_summary(host, PROJECT)
    client = _ListingClient(cast(GitHubClient, cached), summary)
    client.host = host  # type: ignore[attr-defined]
    registry = MockForgeRegistry({host.hostname: client})  # type: ignore[dict-item]
    repo = make_repo(
        tmp_path, PROJECT, hostname=host.hostname, forge_type=host.forge_type
    )
    app, _session, _cache = make_app(tmp_path, [repo], registry, cache=cache)
    return app, forge, http


async def _open_diff(app: TongsApp, pilot: object) -> DiffPanel:
    await _settle(app)
    app.screen.query_one("#reviews-table").focus()
    await pilot.press("enter")  # type: ignore[attr-defined]
    await _settle(app)
    screen = app.screen
    assert isinstance(screen, MRDetailScreen)
    await pilot.press("2")  # type: ignore[attr-defined]
    await _settle(app)
    panel = screen.query_one("#diff-panel", DiffPanel)
    screen.query_one(DiffOptionList).focus()
    await _settle(app)
    return panel


def _unified_lines(app: TongsApp) -> list[DiffLine]:
    option_list = app.screen.query_one(DiffOptionList)
    rows = {**option_list._line_map, **option_list._readonly_rows}
    return [line for _, line in sorted(rows.items())]


def _check_numbers(lines: list[DiffLine]) -> None:
    """Every shown unchanged line carries the head file's numbers and text."""
    for line in lines:
        if line.line_type in (LineType.EXPANDED, LineType.CONTEXT):
            assert line.new_lineno is not None and line.old_lineno is not None
            assert NEW_LINES[line.new_lineno - 1] == line.content
            assert OLD_LINES[line.old_lineno - 1] == line.content


async def _press_e_on_gap(app: TongsApp, pilot: object, position: int) -> None:
    option_list = app.screen.query_one(DiffOptionList)
    option_list.highlighted = sorted(option_list._gap_rows)[position]
    await pilot.press("e")  # type: ignore[attr-defined]
    await _settle(app)


def _header(app: TongsApp) -> str:
    return str(app.screen.query_one("#diff-file-header").render())


HOSTS = [pytest.param(GITHUB, id="github"), pytest.param(GITLAB, id="gitlab")]


@pytest.mark.parametrize("host", HOSTS)
@pytest.mark.asyncio
async def test_expand_between_above_and_below_then_full_file_in_both_layouts(
    tmp_path: Path, host: ForgeHost
) -> None:
    app, forge, http = await _build(tmp_path, host)
    async with http, app.run_test(size=(200, 60), notifications=True) as pilot:
        panel = await _open_diff(app, pilot)
        option_list = app.screen.query_one(DiffOptionList)

        # Opening the diff reads no file, though the diff came over HTTP.
        assert len(option_list._gap_rows) == 3
        assert forge.file_reads == []
        assert any(path.endswith(("/files", "/diffs")) for path in forge.paths)

        # Between the hunks first: one read at the head.
        await _press_e_on_gap(app, pilot, 1)
        assert len(forge.file_reads) == 1
        shown = [line.new_lineno for line in _unified_lines(app)]
        assert [n for n in shown if n is not None and 14 <= n <= 16] == [14, 15, 16]

        # Above and below come from the same read.
        await _press_e_on_gap(app, pilot, 0)
        await _press_e_on_gap(app, pilot, 0)
        assert option_list._gap_rows == {}
        lines = _unified_lines(app)
        assert [line.new_lineno for line in lines if line.new_lineno] == list(
            range(1, 31)
        )
        _check_numbers(lines)
        assert len(forge.file_reads) == 1

        # F after e reads nothing more, in either layout.
        await pilot.press("F")
        await _settle(app)
        assert FULL_FILE_LABEL in _header(app)
        assert len(forge.file_reads) == 1
        panel.request_mode(DiffViewMode.SPLIT)
        await _settle(app)
        assert app.screen.query_one(SplitDiffView).display
        for column, numbers in (
            (app.screen.query_one("#split-old", SplitDiffColumn), OLD_LINES),
            (app.screen.query_one("#split-new", SplitDiffColumn), NEW_LINES),
        ):
            cells = {**column._line_map, **column._readonly_rows}
            side_numbers = [
                (line.old_lineno if column.id == "split-old" else line.new_lineno)
                for _, line in sorted(cells.items())
            ]
            assert [n for n in side_numbers if n is not None] == list(
                range(1, len(numbers) + 1)
            )
        assert len(forge.file_reads) == 1


@pytest.mark.parametrize("host", HOSTS)
@pytest.mark.asyncio
async def test_full_file_first_reads_once_and_e_after_it_reads_nothing(
    tmp_path: Path, host: ForgeHost
) -> None:
    app, forge, http = await _build(tmp_path, host)
    async with http, app.run_test(size=(200, 60), notifications=True) as pilot:
        await _open_diff(app, pilot)
        assert forge.file_reads == []

        await pilot.press("F")
        await _settle(app)
        assert len(forge.file_reads) == 1
        lines = _unified_lines(app)
        assert [line.new_lineno for line in lines if line.new_lineno] == list(
            range(1, 31)
        )
        _check_numbers(lines)

        # Off restores the gaps; e then reads nothing.
        await pilot.press("F")
        await _settle(app)
        assert FULL_FILE_LABEL not in _header(app)
        await _press_e_on_gap(app, pilot, 2)
        assert len(forge.file_reads) == 1
        assert [line.new_lineno for line in _unified_lines(app)][-1] == 30


@pytest.mark.parametrize("host", HOSTS)
@pytest.mark.asyncio
async def test_split_view_expands_from_the_old_column_and_refuses_comments(
    tmp_path: Path, host: ForgeHost
) -> None:
    app, forge, http = await _build(tmp_path, host)
    async with http, app.run_test(size=(200, 60), notifications=True) as pilot:
        panel = await _open_diff(app, pilot)
        panel.request_mode(DiffViewMode.SPLIT)
        await _settle(app)
        old = app.screen.query_one("#split-old", SplitDiffColumn)
        new = app.screen.query_one("#split-new", SplitDiffColumn)
        old.focus()
        await _settle(app)
        assert forge.file_reads == []

        old.highlighted = sorted(old._gap_rows)[1]
        await _settle(app)
        await pilot.press("e")
        await _settle(app)
        assert len(forge.file_reads) == 1
        # The cursor lands on the first new line, in both columns.
        line = old._readonly_rows[old.highlighted]
        assert (line.old_lineno, line.new_lineno) == (14, 14)
        assert new.highlighted == old.highlighted

        await pilot.press("c")
        await _settle(app)
        messages = [n.message for n in app._notifications]
        assert READ_ONLY_LINE_MESSAGE in messages
        assert all(
            line.content == NEW_LINES[line.new_lineno - 1]
            for line in new._readonly_rows.values()
            if line.new_lineno is not None
        )
