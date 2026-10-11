"""Generating a truncated file's diff locally, end to end in the terminal (#382).

The forge is an ``httpx.MockTransport`` that serves one review whose only file
has no diff: GitHub leaves out its ``patch`` and GitLab marks it
``too_large``. The real session and clients download both versions, so the
tests check the request cost (two file reads, plus one compare on GitHub and
never a read at GitHub's base branch tip) as well as the line numbers.
Failure and stale-reply cases swap the adapter's generation call for a fake.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import cast

import httpx
import pytest

from tests.test_diff.test_context import FIXTURES
from tests.test_tui_diff_context import _ListingClient, _open_diff
from tests.test_tui_session import MockForgeRegistry, make_app, make_repo, make_summary
from tongs.app import TongsApp
from tongs.cache.cached_client import CachedForgeClient
from tongs.cache.store import CacheStore
from tongs.diff.models import DiffFile, DiffLine, LineType
from tongs.forges.github import GitHubClient
from tongs.forges.gitlab import GitLabClient
from tongs.forges.models import ForgeHost, MRSummary
from tongs.scanner.repo import ForgeType
from tongs.services.models import ReviewRevision
from tongs.services.review_files import (
    GENERATE_BINARY_MESSAGE,
    NO_MERGE_BASE_MESSAGE,
    GeneratedDiff,
    GenerationStatus,
    generate_too_large_message,
)
from tongs.views.mr_detail import MRDetailScreen
from tongs.widgets.comment_editor import CommentEditor
from tongs.widgets.diff_panel import (
    GENERATED_LABEL,
    GENERATION_CANCELLED_MESSAGE,
    CommentMode,
    CommentRequested,
    DiffOptionList,
)
from tongs.widgets.split_diff import (
    GENERATED_COMMENT_MESSAGE,
    TRUNCATED_NOTICE,
    DiffViewMode,
    SplitDiffColumn,
)

PROJECT = "acme/widgets"
PATH = "src/pkg/widget notes.txt"
HEAD = "c0ffee" + "0" * 34
BASE = "ba5e" + "0" * 36
MERGE_BASE = "3e7b" + "0" * 36
CREATED = "2026-10-01T12:00:00Z"
OLD_TEXT, NEW_TEXT, _HUNKS = FIXTURES["two_hunks"]
OLD_LINES = OLD_TEXT.split("\n")[:-1]
NEW_LINES = NEW_TEXT.split("\n")[:-1]
GENERATING = "Generating diff locally (downloading 2 files). Press e again to cancel."

GITHUB = ForgeHost("github.com", ForgeType.GITHUB, "https://api.github.com")
GITLAB = ForgeHost("gitlab.com", ForgeType.GITLAB, "https://gitlab.com/api/v4")
HOSTS = [pytest.param(GITHUB, id="github"), pytest.param(GITLAB, id="gitlab")]


@dataclass
class _Forge:
    """What the mocked forge served, and a gate that can hold file reads."""

    file_reads: list[str] = field(default_factory=list)
    compares: list[str] = field(default_factory=list)
    gate: asyncio.Event | None = None
    waiting: int = 0


def _json(payload: object) -> httpx.Response:
    return httpx.Response(200, content=json.dumps(payload).encode())


async def _file_body(forge: _Forge, ref: str, old_ref: str) -> httpx.Response:
    forge.file_reads.append(ref)
    if forge.gate is not None:
        forge.waiting += 1
        await forge.gate.wait()
    if ref == old_ref:
        return httpx.Response(200, content=OLD_TEXT.encode())
    assert ref == HEAD
    return httpx.Response(200, content=NEW_TEXT.encode())


def _github_handler(
    forge: _Forge,
) -> Callable[[httpx.Request], Awaitable[httpx.Response]]:
    pull = f"/repos/{PROJECT}/pulls/7"
    contents = f"/repos/{PROJECT}/contents/src/pkg/widget%20notes.txt"
    compare = f"/repos/{PROJECT}/compare/{BASE}...{HEAD}"

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        raw_path = request.url.raw_path.decode().split("?")[0]
        if raw_path == contents:
            ref = request.url.params["ref"]
            # base.sha is the base branch tip on GitHub, never the old side.
            assert ref != BASE
            return await _file_body(forge, ref, MERGE_BASE)
        if path == compare:
            forge.compares.append(path)
            return _json({"merge_base_commit": {"sha": MERGE_BASE}})
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
            # GitHub leaves out the patch of a file whose diff is too large.
            return _json(
                [
                    {
                        "filename": PATH,
                        "status": "modified",
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


def _gitlab_handler(
    forge: _Forge,
) -> Callable[[httpx.Request], Awaitable[httpx.Response]]:
    project = "acme%2Fwidgets"
    mr = f"/api/v4/projects/{project}/merge_requests/7"
    raw = f"/api/v4/projects/{project}/repository/files/src%2Fpkg%2Fwidget%20notes.txt/raw"

    async def handler(request: httpx.Request) -> httpx.Response:
        raw_path = request.url.raw_path.decode().split("?")[0]
        if raw_path == raw:
            return await _file_body(forge, request.url.params["ref"], BASE)
        if "/compare" in raw_path:
            forge.compares.append(raw_path)
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
            # GitLab sends an empty diff and too_large for an oversized file.
            return _json(
                [
                    {
                        "old_path": PATH,
                        "new_path": PATH,
                        "diff": "",
                        "too_large": True,
                        "collapsed": False,
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


async def _build(
    tmp_path: Path, host: ForgeHost
) -> tuple[TongsApp, _Forge, httpx.AsyncClient]:
    forge = _Forge()
    handler = (
        _github_handler(forge)
        if host.forge_type is ForgeType.GITHUB
        else _gitlab_handler(forge)
    )
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
    summary: MRSummary = make_summary(host, PROJECT)
    client = _ListingClient(cast(GitHubClient, cached), summary)
    client.host = host  # type: ignore[attr-defined]
    registry = MockForgeRegistry({host.hostname: client})  # type: ignore[dict-item]
    repo = make_repo(
        tmp_path, PROJECT, hostname=host.hostname, forge_type=host.forge_type
    )
    app, _session, _cache = make_app(tmp_path, [repo], registry, cache=cache)
    return app, forge, http


async def _until(pilot: object, predicate: Callable[[], bool]) -> None:
    """Pump the app until ``predicate`` holds, without waiting on workers."""
    async with asyncio.timeout(5):
        while not predicate():
            await pilot.pause()  # type: ignore[attr-defined]


def _rows(app: TongsApp) -> list[str]:
    option_list = app.screen.query_one(DiffOptionList)
    return [
        option_list.get_option_at_index(index).prompt.plain
        for index in range(option_list.option_count)
    ]


def _header(app: TongsApp) -> str:
    return str(app.screen.query_one("#diff-file-header").render())


def _messages(app: TongsApp) -> list[str]:
    return [notification.message for notification in app._notifications]


def _check_numbers(lines: list[DiffLine]) -> None:
    """Every shown line carries the numbers of its text in the two versions."""
    for line in lines:
        if line.line_type in (LineType.CONTEXT, LineType.DELETION, LineType.EXPANDED):
            assert line.old_lineno is not None
            assert OLD_LINES[line.old_lineno - 1] == line.content
        if line.line_type in (LineType.CONTEXT, LineType.ADDITION, LineType.EXPANDED):
            assert line.new_lineno is not None
            assert NEW_LINES[line.new_lineno - 1] == line.content


def _unified_lines(app: TongsApp) -> list[DiffLine]:
    option_list = app.screen.query_one(DiffOptionList)
    rows = {**option_list._line_map, **option_list._readonly_rows}
    return [line for _, line in sorted(rows.items())]


def _wait_for_generation(app: TongsApp) -> Callable[[], bool]:
    return lambda: GENERATED_LABEL in _header(app)


@pytest.mark.parametrize("host", HOSTS)
@pytest.mark.asyncio
async def test_withheld_file_generates_and_renders_in_both_layouts(
    tmp_path: Path, host: ForgeHost
) -> None:
    app, forge, http = await _build(tmp_path, host)
    async with http, app.run_test(size=(200, 60), notifications=True) as pilot:
        panel = await _open_diff(app, pilot)

        # Opening the diff reads no file and shows the notice.
        assert _rows(app) == [TRUNCATED_NOTICE]
        assert forge.file_reads == []
        assert forge.compares == []

        await pilot.press("e")
        await _until(pilot, _wait_for_generation(app))

        # Two file reads; GitHub also asks for the merge base once.
        old_ref = MERGE_BASE if host is GITHUB else BASE
        assert sorted(forge.file_reads) == sorted([old_ref, HEAD])
        assert len(forge.compares) == (1 if host is GITHUB else 0)
        file = panel._files[0]
        assert file.generated_locally and not file.is_truncated
        screen = app.screen
        assert isinstance(screen, MRDetailScreen)
        assert screen._cached_diff_files is not None
        assert screen._cached_diff_files[0] is file

        lines = _unified_lines(app)
        _check_numbers(lines)
        assert [
            line.new_lineno for line in lines if line.line_type is LineType.ADDITION
        ] == [10, 20]
        assert all(row.isascii() for row in _rows(app))

        # Gaps open from the downloaded head version, with no new read.
        option_list = app.screen.query_one(DiffOptionList)
        option_list.highlighted = min(option_list._gap_rows)
        await pilot.press("e")
        await pilot.pause()
        _check_numbers(_unified_lines(app))
        assert [n.new_lineno for n in _unified_lines(app)][:6] == [1, 2, 3, 4, 5, 6]
        assert len(forge.file_reads) == 2

        panel.request_mode(DiffViewMode.SPLIT)
        await pilot.pause()
        for side, version in (("#split-old", OLD_LINES), ("#split-new", NEW_LINES)):
            column = app.screen.query_one(side, SplitDiffColumn)
            cells = {**column._line_map, **column._readonly_rows}
            for _, line in sorted(cells.items()):
                number = line.old_lineno if side == "#split-old" else line.new_lineno
                if number is not None:
                    assert version[number - 1] == line.content
        assert GENERATED_LABEL in _header(app)
        assert len(forge.file_reads) == 2


@pytest.mark.parametrize("host", HOSTS)
@pytest.mark.asyncio
async def test_e_again_cancels_the_download_and_e_retries(
    tmp_path: Path, host: ForgeHost
) -> None:
    app, forge, http = await _build(tmp_path, host)
    async with http, app.run_test(size=(200, 60), notifications=True) as pilot:
        panel = await _open_diff(app, pilot)
        forge.gate = asyncio.Event()

        await pilot.press("e")
        await _until(pilot, lambda: forge.waiting == 2)
        assert _rows(app) == [GENERATING]

        await pilot.press("e")
        await pilot.pause()
        assert _rows(app) == [TRUNCATED_NOTICE, GENERATION_CANCELLED_MESSAGE]
        forge.gate.set()
        for _ in range(5):
            await pilot.pause()
        assert panel._files[0].is_truncated
        assert GENERATED_LABEL not in _header(app)
        assert _rows(app) == [TRUNCATED_NOTICE, GENERATION_CANCELLED_MESSAGE]

        # Nothing was kept from the cancelled try, so e downloads again.
        forge.gate = None
        await pilot.press("e")
        await _until(pilot, _wait_for_generation(app))
        assert len(forge.file_reads) == 4
        _check_numbers(_unified_lines(app))


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (
            GeneratedDiff(
                GenerationStatus.TOO_LARGE, None, None, generate_too_large_message()
            ),
            generate_too_large_message(),
        ),
        (
            GeneratedDiff(GenerationStatus.BINARY, None, None, GENERATE_BINARY_MESSAGE),
            GENERATE_BINARY_MESSAGE,
        ),
        (
            GeneratedDiff(
                GenerationStatus.NO_MERGE_BASE, None, None, NO_MERGE_BASE_MESSAGE
            ),
            NO_MERGE_BASE_MESSAGE,
        ),
        (
            OSError("connection reset"),
            "Could not generate the diff. (connection reset)",
        ),
    ],
    ids=["over-cap", "binary", "no-merge-base", "network"],
)
@pytest.mark.asyncio
async def test_each_failure_restores_the_notice_with_its_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    result: GeneratedDiff | Exception,
    message: str,
) -> None:
    app, forge, http = await _build(tmp_path, GITHUB)
    calls: list[DiffFile] = []

    async def fake(
        summary: MRSummary, revision: ReviewRevision, file: DiffFile
    ) -> GeneratedDiff:
        calls.append(file)
        if isinstance(result, Exception):
            raise result
        return result

    async with http, app.run_test(size=(200, 60), notifications=True) as pilot:
        panel = await _open_diff(app, pilot)
        monkeypatch.setattr(app.services, "generate_file_diff", fake)

        await pilot.press("e")
        await _until(pilot, lambda: message in _rows(app))

        assert calls == [panel._files[0]]
        assert _rows(app) == [TRUNCATED_NOTICE, message]
        assert message in _messages(app)
        assert all(row.isascii() for row in _rows(app))
        assert panel._files[0].is_truncated
        assert forge.file_reads == []


@pytest.mark.asyncio
async def test_a_result_for_a_refreshed_review_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _forge, http = await _build(tmp_path, GITHUB)
    release = asyncio.Event()
    started = asyncio.Event()

    async def fake(
        summary: MRSummary, revision: ReviewRevision, file: DiffFile
    ) -> GeneratedDiff:
        started.set()
        await release.wait()
        return GeneratedDiff(
            GenerationStatus.OK,
            replace(file, hunks=(), is_truncated=False, generated_locally=True),
            None,
            "",
        )

    async with http, app.run_test(size=(200, 60), notifications=True) as pilot:
        panel = await _open_diff(app, pilot)
        monkeypatch.setattr(app.services, "generate_file_diff", fake)
        screen = app.screen
        assert isinstance(screen, MRDetailScreen)
        truncated = panel._files[0]

        await pilot.press("e")
        await _until(pilot, started.is_set)
        # A refresh replaces the displayed revision while the diff downloads.
        revision = screen._displayed_diff_revision
        assert revision is not None
        screen._displayed_diff_revision = replace(revision)
        release.set()
        await _until(pilot, lambda: _rows(app) == [TRUNCATED_NOTICE])

        assert panel._files[0] is truncated
        assert GENERATED_LABEL not in _header(app)


@pytest.mark.asyncio
async def test_ctrl_r_cancels_a_running_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _forge, http = await _build(tmp_path, GITHUB)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def fake(
        summary: MRSummary, revision: ReviewRevision, file: DiffFile
    ) -> GeneratedDiff:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        raise AssertionError("unreachable")

    async with http, app.run_test(size=(200, 60), notifications=True) as pilot:
        await _open_diff(app, pilot)
        monkeypatch.setattr(app.services, "generate_file_diff", fake)

        await pilot.press("e")
        await _until(pilot, started.is_set)
        await pilot.press("ctrl+r")
        await _until(pilot, cancelled.is_set)
        await _until(
            pilot,
            lambda: _rows(app) == [TRUNCATED_NOTICE, GENERATION_CANCELLED_MESSAGE],
        )


@pytest.mark.parametrize("host", HOSTS)
@pytest.mark.asyncio
async def test_comments_on_a_generated_diff_never_reach_the_editor(
    tmp_path: Path, host: ForgeHost
) -> None:
    app, _forge, http = await _build(tmp_path, host)
    async with http, app.run_test(size=(200, 60), notifications=True) as pilot:
        panel = await _open_diff(app, pilot)
        await pilot.press("e")
        await _until(pilot, _wait_for_generation(app))
        editor = app.screen.query_one("#comment-editor", CommentEditor)
        option_list = app.screen.query_one(DiffOptionList)
        option_list.focus()
        line = option_list._line_map[option_list.highlighted]
        assert line.line_type in (LineType.ADDITION, LineType.DELETION)

        await pilot.press("c")
        await pilot.pause()
        assert not editor.display
        assert GENERATED_COMMENT_MESSAGE in _messages(app)

        # The screen refuses too, which also covers review-draft mode and the
        # panel's fallback comment action.
        before = _messages(app).count(GENERATED_COMMENT_MESSAGE)
        for mode in (CommentMode.COMMENT, CommentMode.SUGGEST):
            panel.post_message(
                CommentRequested(file=panel._files[0], line=line, mode=mode)
            )
            await pilot.pause()
        assert not editor.display
        assert _messages(app).count(GENERATED_COMMENT_MESSAGE) == before + 2


@pytest.mark.asyncio
async def test_a_quick_start_cancel_start_ends_generated_not_cancelled(
    tmp_path: Path,
) -> None:
    app, _forge, http = await _build(tmp_path, GITHUB)
    async with http, app.run_test(size=(200, 60), notifications=True) as pilot:
        panel = await _open_diff(app, pilot)
        truncated = panel._files[0]

        # Three presses land before the screen handles any of their messages:
        # start, cancel, start. The cancel must stop only the first download.
        for _ in range(3):
            panel.action_expand_context()
        assert panel.is_generating(truncated)

        await _until(pilot, _wait_for_generation(app))
        assert panel._files[0].generated_locally
        assert GENERATION_CANCELLED_MESSAGE not in _rows(app)
        _check_numbers(_unified_lines(app))


@pytest.mark.parametrize("same_files", [False, True], ids=["rebuilt", "copied"])
@pytest.mark.asyncio
async def test_generation_finds_the_file_in_the_panel_list(
    tmp_path: Path, same_files: bool
) -> None:
    app, _forge, http = await _build(tmp_path, GITHUB)
    async with http, app.run_test(size=(200, 60), notifications=True) as pilot:
        panel = await _open_diff(app, pilot)
        screen = app.screen
        assert isinstance(screen, MRDetailScreen)
        truncated = panel._files[0]
        # Loading discussions after a refresh can rebuild the screen's list
        # from its own diff read, with other file objects.
        cached = list(panel._files) if same_files else [replace(truncated)]
        screen._cached_diff_files = cached

        await pilot.press("e")
        await _until(pilot, _wait_for_generation(app))

        generated = panel._files[0]
        assert generated.generated_locally
        if same_files:
            assert cached[0] is generated
        else:
            assert cached[0] is not generated
            assert cached[0].is_truncated
