"""All Open rows appear per repository as each read finishes (issue #293).

The tab no longer waits for the slowest repository. A status line counts the
finished repositories and gives way to the summary, and to any per-repository
warnings, once every read is done. The rows keep the active sort, the cursor
stays on its merge request, and a new load never receives an older load's rows.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from textual.pilot import Pilot
from textual.widgets import Static, TabbedContent

from tests.test_tui_session import (
    NOW,
    MockForgeClient,
    MockForgeRegistry,
    make_app,
    make_repo,
    make_summary,
    settle,
)
from tongs.app import TongsApp
from tongs.errors import NotFoundError
from tongs.forges.models import ForgeHost, MRSummary
from tongs.scanner.repo import ForgeType
from tongs.services.errors import ServiceError, ServiceErrorCode
from tongs.services.models import ReviewPage, ReviewScope
from tongs.tui_services import TUIRepositoryProgress
from tongs.widgets.mr_table import MRTable

HOST = ForgeHost("github.com", ForgeType.GITHUB, "https://api.github.com")


class GatedForgeClient(MockForgeClient):
    """Forge double whose repository reads wait until the test releases them.

    A read captures its result when it starts, so a read that finished after a
    refresh would still return the older list.
    """

    def __init__(self, summaries: list[MRSummary]) -> None:
        super().__init__(HOST, summaries)
        self.held: set[str] = set()
        self.gates: dict[str, asyncio.Event] = {}
        self.started: dict[str, asyncio.Event] = {}
        self.cancelled: list[str] = []

    def gate(self, repo_path: str) -> asyncio.Event:
        return self.gates.setdefault(repo_path, asyncio.Event())

    def start(self, repo_path: str) -> asyncio.Event:
        return self.started.setdefault(repo_path, asyncio.Event())

    async def list_mrs(
        self, repo_path: str, state: str = "open", per_page: int = 100
    ) -> list[MRSummary]:
        result = [
            summary for summary in self.summaries if summary.repo_path == repo_path
        ]
        self.calls.append(("all_open", repo_path))
        self.start(repo_path).set()
        if repo_path in self.held:
            try:
                await self.gate(repo_path).wait()
            except asyncio.CancelledError:
                self.cancelled.append(repo_path)
                raise
        if repo_path in self.repo_errors:
            raise self.repo_errors[repo_path]
        return result


def summary(project: str, number: int, age_hours: int, title: str = "") -> MRSummary:
    return replace(
        make_summary(HOST, project, number),
        title=title or f"Review {project} {number}",
        updated_at=NOW - timedelta(hours=age_hours),
    )


def numbers(table: MRTable) -> list[int]:
    return [table._mr_data[row.key.value].number for row in table.ordered_rows]


def status_text(app: TongsApp) -> str:
    return str(app.screen.query_one("#all-open-status", Static).render())


def all_open_notices(app: TongsApp) -> list[str]:
    return [
        str(notice.message)
        for notice in app._notifications
        if str(notice.message).startswith("All Open")
    ]


async def wait_until(pilot: Pilot, condition: Callable[[], bool]) -> None:
    async def poll() -> None:
        while not condition():
            await pilot.pause()

    await asyncio.wait_for(poll(), timeout=5)


def build(tmp_path: Path, client: GatedForgeClient, projects: list[str]) -> TongsApp:
    registry = MockForgeRegistry({HOST.hostname: client})
    repos = [make_repo(tmp_path, project) for project in projects]
    app, _session, _cache = make_app(tmp_path, repos, registry)
    return app


@pytest.mark.asyncio
async def test_first_repository_rows_show_before_a_slow_repository_finishes(
    tmp_path: Path,
) -> None:
    client = GatedForgeClient([summary("acme/fast", 1, 5), summary("acme/slow", 2, 1)])
    client.held.add("acme/slow")
    app = build(tmp_path, client, ["acme/fast", "acme/slow"])

    async with app.run_test(notifications=True) as pilot:
        await settle(app)
        await pilot.press("3")
        table = app.screen.query_one("#all-open-table", MRTable)
        await wait_until(pilot, lambda: table.row_count == 1)

        assert numbers(table) == [1]
        assert table.loading is False
        assert not client.gate("acme/slow").is_set()
        assert status_text(app) == "Loading 1 of 2 repositories"

        client.gate("acme/slow").set()
        await settle(app)

        # The newer merge request sorts above the one already shown.
        assert numbers(table) == [2, 1]
        assert status_text(app) == "2 open MRs across 2 repositories"
        assert all_open_notices(app) == []


@pytest.mark.asyncio
async def test_progress_counts_up_then_gives_way_to_the_summary(
    tmp_path: Path,
) -> None:
    projects = ["acme/one", "acme/two", "acme/three"]
    client = GatedForgeClient(
        [
            summary(project, index, index)
            for index, project in enumerate(projects, start=1)
        ]
    )
    client.held.update(projects)
    app = build(tmp_path, client, projects)

    async with app.run_test(notifications=True) as pilot:
        await settle(app)
        await pilot.press("3")
        await wait_until(pilot, lambda: client.start("acme/one").is_set())
        await pilot.pause()
        seen = [status_text(app)]

        for count, project in enumerate(projects, start=1):
            await wait_until(
                pilot, lambda project=project: client.start(project).is_set()
            )
            client.gate(project).set()
            await wait_until(
                pilot,
                lambda count=count: (
                    app.screen.query_one("#all-open-table", MRTable).row_count == count
                ),
            )
            seen.append(status_text(app))

        assert seen == [
            "Loading 0 of 3 repositories",
            "Loading 1 of 3 repositories",
            "Loading 2 of 3 repositories",
            "3 open MRs across 3 repositories",
        ]


@pytest.mark.asyncio
async def test_cursor_stays_on_its_merge_request_when_rows_land_above(
    tmp_path: Path,
) -> None:
    client = GatedForgeClient(
        [
            summary("acme/fast", 1, 10),
            summary("acme/fast", 2, 20),
            summary("acme/fast", 3, 30),
            summary("acme/slow", 4, 1),
            summary("acme/slow", 5, 25),
        ]
    )
    client.held.add("acme/slow")
    app = build(tmp_path, client, ["acme/fast", "acme/slow"])

    async with app.run_test(notifications=True) as pilot:
        await settle(app)
        await pilot.press("3")
        table = app.screen.query_one("#all-open-table", MRTable)
        await wait_until(pilot, lambda: table.row_count == 3)
        assert numbers(table) == [1, 2, 3]
        table.focus()
        await pilot.press("down")
        selected = table.get_selected_mr()
        assert selected is not None
        assert selected.number == 2

        client.gate("acme/slow").set()
        await wait_until(pilot, lambda: table.row_count == 5)

        assert numbers(table) == [4, 1, 2, 5, 3]
        selected = table.get_selected_mr()
        assert selected is not None
        assert selected.number == 2
        assert table.cursor_row == 2


@pytest.mark.asyncio
async def test_rows_keep_the_active_sort_as_they_arrive(tmp_path: Path) -> None:
    client = GatedForgeClient(
        [
            summary("acme/fast", 1, 10, "Charlie"),
            summary("acme/fast", 2, 20, "Alpha"),
            summary("acme/slow", 3, 1, "Bravo"),
        ]
    )
    client.held.add("acme/slow")
    app = build(tmp_path, client, ["acme/fast", "acme/slow"])

    async with app.run_test(notifications=True) as pilot:
        await settle(app)
        await pilot.press("3")
        table = app.screen.query_one("#all-open-table", MRTable)
        await wait_until(pilot, lambda: table.row_count == 2)
        table.focus()
        await pilot.press("s")
        assert numbers(table) == [2, 1]
        await pilot.press("down")

        client.gate("acme/slow").set()
        await wait_until(pilot, lambda: table.row_count == 3)

        assert numbers(table) == [2, 3, 1]
        selected = table.get_selected_mr()
        assert selected is not None
        assert selected.number == 1


@pytest.mark.asyncio
async def test_refresh_mid_load_discards_the_older_load(tmp_path: Path) -> None:
    client = GatedForgeClient([summary("acme/fast", 1, 5), summary("acme/slow", 2, 1)])
    client.held.add("acme/slow")
    app = build(tmp_path, client, ["acme/fast", "acme/slow"])

    async with app.run_test(notifications=True) as pilot:
        await settle(app)
        await pilot.press("3")
        table = app.screen.query_one("#all-open-table", MRTable)
        await wait_until(pilot, lambda: table.row_count == 1)

        # The refresh sees new lists; the held read still holds the old one.
        client.summaries = [summary("acme/fast", 11, 5), summary("acme/slow", 12, 1)]
        client.started.clear()
        stale_gate = client.gates.pop("acme/slow")
        await pilot.press("ctrl+r")
        await wait_until(pilot, lambda: client.cancelled == ["acme/slow"])
        stale_gate.set()
        await wait_until(pilot, lambda: client.start("acme/slow").is_set())
        await wait_until(pilot, lambda: numbers(table) == [11])
        assert status_text(app) == "Loading 1 of 2 repositories"

        client.gate("acme/slow").set()
        await settle(app)

        assert numbers(table) == [12, 11]
        assert status_text(app) == "2 open MRs across 2 repositories"


@pytest.mark.asyncio
async def test_leaving_the_tab_mid_load_stops_it_and_returning_reloads(
    tmp_path: Path,
) -> None:
    client = GatedForgeClient([summary("acme/fast", 1, 5), summary("acme/slow", 2, 1)])
    client.held.add("acme/slow")
    app = build(tmp_path, client, ["acme/fast", "acme/slow"])

    async with app.run_test(notifications=True) as pilot:
        await settle(app)
        await pilot.press("3")
        table = app.screen.query_one("#all-open-table", MRTable)
        await wait_until(pilot, lambda: table.row_count == 1)

        await pilot.press("1")
        await wait_until(pilot, lambda: client.cancelled == ["acme/slow"])
        assert status_text(app) == ""
        assert app.screen.query_one(TabbedContent).active == "reviews"

        client.held.clear()
        await pilot.press("3")
        await settle(app)

        assert numbers(table) == [2, 1]
        assert status_text(app) == "2 open MRs across 2 repositories"
        reads = [path for name, path in client.calls if name == "all_open"]
        assert reads.count("acme/slow") == 2


@pytest.mark.asyncio
async def test_failures_still_warn_once_every_repository_is_done(
    tmp_path: Path,
) -> None:
    client = GatedForgeClient([summary("acme/fast", 1, 5), summary("acme/slow", 2, 1)])
    client.repo_errors["acme/broken"] = NotFoundError("private forge detail")
    client.held.add("acme/slow")
    app = build(tmp_path, client, ["acme/broken", "acme/fast", "acme/slow"])

    async with app.run_test(notifications=True) as pilot:
        await settle(app)
        app.clear_notifications()
        await pilot.press("3")
        table = app.screen.query_one("#all-open-table", MRTable)
        await wait_until(pilot, lambda: table.row_count == 1)
        await wait_until(
            pilot, lambda: status_text(app) == "Loading 2 of 3 repositories"
        )
        assert all_open_notices(app) == []

        client.gate("acme/slow").set()
        await settle(app)

        assert numbers(table) == [2, 1]
        assert status_text(app) == "2 open MRs across 3 repositories"
        expected = (
            "All Open skipped: github.com/acme/broken: "
            "The requested forge resource was not found."
        )
        assert all_open_notices(app) == [expected]


@pytest.mark.asyncio
async def test_each_repository_builds_only_its_own_rows(tmp_path: Path) -> None:
    # Every repository's merge requests fall in among the rows already shown,
    # so a table that rebuilt every row per repository would build about
    # repositories * rows / 2 rows here instead of one per merge request.
    repositories = 40
    per_repository = 5
    projects = [f"acme/repo{index}" for index in range(repositories)]
    client = GatedForgeClient(
        [
            summary(project, offset * 100 + index + 1, offset * repositories + index)
            for index, project in enumerate(projects)
            for offset in range(per_repository)
        ]
    )
    app = build(tmp_path, client, projects)
    built: list[str] = []
    clears: list[bool] = []
    merges: list[int] = []

    async with app.run_test(notifications=True) as pilot:
        await settle(app)
        table = app.screen.query_one("#all-open-table", MRTable)
        add_row = table.add_row
        clear = table.clear
        merge_mrs = table.merge_mrs

        def counting_add_row(*cells: object, **kwargs: object) -> object:
            built.append(str(kwargs["key"]))
            return add_row(*cells, **kwargs)

        def counting_clear(*args: object, **kwargs: object) -> MRTable:
            clears.append(True)
            return clear(*args, **kwargs)

        def counting_merge(*args: object, **kwargs: object) -> None:
            merges.append(table.row_count)
            merge_mrs(*args, **kwargs)

        table.add_row = counting_add_row
        table.clear = counting_clear
        table.merge_mrs = counting_merge
        await pilot.press("3")
        await settle(app)

        total = repositories * per_repository
        assert table.row_count == total
        assert (
            status_text(app) == f"{total} open MRs across {repositories} repositories"
        )
        assert len(built) == total
        assert len(set(built)) == total
        # One step per repository, each landing among the rows already shown.
        assert len(merges) == repositories
        assert len(clears) == 1
        assert all_open_notices(app) == []
        # The rows were moved, not rebuilt, and still read newest first.
        ordered = [table._mr_data[row.key.value] for row in table.ordered_rows]
        ages = [NOW - mr.updated_at for mr in ordered]
        assert ages == sorted(ages)
        for index, row in enumerate(table.ordered_rows):
            assert table.get_row_index(row.key) == index
            number = str(table.get_row_at(index)[2])
            assert number == str(table._mr_data[row.key.value].number)


@pytest.mark.asyncio
async def test_a_repeated_merge_request_replaces_its_row(tmp_path: Path) -> None:
    client = GatedForgeClient([summary("acme/fast", 1, 5), summary("acme/slow", 2, 1)])
    app = build(tmp_path, client, ["acme/fast", "acme/slow"])

    async with app.run_test(notifications=True) as pilot:
        await settle(app)
        await pilot.press("3")
        await settle(app)
        table = app.screen.query_one("#all-open-table", MRTable)
        assert numbers(table) == [2, 1]
        table.focus()
        await pilot.press("down")

        table.merge_mrs(
            [summary("acme/fast", 1, 0, "Edited"), summary("acme/fast", 3, 3)]
        )

        assert numbers(table) == [1, 2, 3]
        assert table.row_count == 3
        assert str(table.get_row_at(0)[3]).strip() == "Edited"
        selected = table.get_selected_mr()
        assert selected is not None
        assert selected.number == 1
        assert selected.title == "Edited"


@pytest.mark.asyncio
async def test_a_refresh_where_every_repository_fails_says_the_rows_are_old(
    tmp_path: Path,
) -> None:
    client = GatedForgeClient([summary("acme/a", 1, 2), summary("acme/b", 2, 1)])
    app = build(tmp_path, client, ["acme/a", "acme/b"])

    async with app.run_test(notifications=True) as pilot:
        await settle(app)
        await pilot.press("3")
        await settle(app)
        table = app.screen.query_one("#all-open-table", MRTable)
        assert numbers(table) == [2, 1]

        client.repo_errors["acme/a"] = NotFoundError("gone")
        client.repo_errors["acme/b"] = NotFoundError("gone")
        app.screen.load_all_open(fresh=True)
        await settle(app)

        assert numbers(table) == [2, 1]
        assert status_text(app) == (
            "Showing earlier results: 2 of 2 repositories could not be loaded"
        )


@pytest.mark.asyncio
async def test_a_stream_error_after_some_rows_keeps_the_warnings_and_says_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = GatedForgeClient([summary("acme/a", 1, 2)])
    client.repo_errors["acme/b"] = NotFoundError("gone")
    app = build(tmp_path, client, ["acme/a", "acme/b"])

    async with app.run_test(notifications=True) as pilot:
        await settle(app)
        page = await app.services.list_reviews(ReviewScope.ALL_OPEN)
        assert len(page.items) == 1
        assert len(page.failures) == 1

        async def broken_stream(*, fresh: bool = False):
            yield TUIRepositoryProgress(None, ReviewPage((), ()), 0, 3)
            yield TUIRepositoryProgress(None, page, 2, 3)
            raise ServiceError(ServiceErrorCode.CONFLICT, "The read stopped.")

        monkeypatch.setattr(app.services, "stream_all_open", broken_stream)
        await pilot.press("3")
        await settle(app)

        table = app.screen.query_one("#all-open-table", MRTable)
        assert numbers(table) == [1]
        assert status_text(app) == "List incomplete: 1 open MR loaded"
        notices = all_open_notices(app)
        assert "All Open: The read stopped." in notices
        assert any("acme/b" in notice for notice in notices)
