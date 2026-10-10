"""MR inbox dashboard -- the default screen."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Sequence
from contextlib import aclosing, suppress
from dataclasses import dataclass, field
from typing import ClassVar

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.css.query import NoMatches
from textual.dom import NoScreen
from textual.reactive import reactive
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static, TabbedContent, TabPane

from tongs.forges.models import CIStatus, MRSummary
from tongs.scanner.repo import Repo
from tongs.services.errors import ServiceError, ServiceErrorCode
from tongs.services.models import (
    HostFailure,
    RepositoryRef,
    ReviewListItem,
    ReviewScope,
)
from tongs.tui_services import TUIRepositoryProgress
from tongs.widgets.mr_table import MRTable, mr_row_key

# Each inbox tab and the table that shows it.
_TAB_TABLES = {
    "reviews": "#reviews-table",
    "my-mrs": "#my-mrs-table",
    "all-open": "#all-open-table",
}


@dataclass
class _CILookups:
    """The per-row CI lookups of one inbox tab.

    Each reload or stop of the tab takes the next generation, and a lookup of
    an older one never lands. Each queue entry says whether its lookup reads
    past the CI cache.
    """

    group: str
    generation: int = 0
    queue: deque[tuple[MRSummary, bool]] = field(default_factory=deque)
    queued: set[str] = field(default_factory=set)
    workers: int = 0


class InboxScreen(Screen):
    """MR inbox dashboard with tabs for My Reviews / My MRs / All Open.

    When `repo` is provided, the inbox is scoped to that single repo.
    """

    DEFAULT_CSS = """
    InboxScreen #all-open-status {
        height: 1;
        background: $accent 15%;
        padding: 0 1;
    }
    """

    BINDINGS: ClassVar[list] = [
        Binding("r", "repos_or_back", "Repos", show=True, key_display="r"),
        Binding("1", "focus_tab('reviews')", "Reviews", show=False),
        Binding("2", "focus_tab('my-mrs')", "My MRs", show=False),
        Binding("3", "focus_tab('all-open')", "All Open", show=False),
        Binding("o", "open_in_browser", "Open", show=True),
        Binding("m", "load_more", "More", show=True),
        Binding("ctrl+r", "refresh", "Refresh", show=True),
        Binding("escape", "go_back", "Back", show=False),
        Binding("q", "quit_or_back", "Quit", show=True),
    ]

    loading_reviews: reactive[bool] = reactive(False)
    loading_my_mrs: reactive[bool] = reactive(False)
    loading_all_open: reactive[bool] = reactive(False)

    def __init__(self, repo: Repo | None = None):
        super().__init__()
        self.scoped_repo = repo
        self._loaded_tabs: set[str] = set()
        self._repository_generation = 0
        # Each All Open load takes the next number; a load whose number is no
        # longer current has been replaced or stopped and must not touch the tab.
        self._all_open_load = 0
        self._all_open_pending = False
        # The next page of each All Open repository that has more to show.
        self._all_open_cursors: dict[RepositoryRef, str] = {}
        self._all_open_total = 0
        # Rows of every tab arrive without CI and are filled in per row.
        self._ci = {tab_id: _CILookups(f"{tab_id}-ci") for tab_id in _TAB_TABLES}

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(initial="reviews"):
            with TabPane("My Reviews", id="reviews"):
                yield MRTable(id="reviews-table")
            with TabPane("My MRs", id="my-mrs"):
                yield MRTable(id="my-mrs-table")
            with TabPane("All Open", id="all-open"):
                yield Static("", id="all-open-status")
                yield MRTable(id="all-open-table")
        yield Footer()

    def on_mount(self) -> None:
        self._repository_generation = self.app.repository_generation
        show_repo = self.scoped_repo is None
        for table in self.query(MRTable):
            table.setup_columns(show_repo=show_repo)
            table.cursor_type = "row"
            table.zebra_stripes = True
        if self.scoped_repo:
            self.sub_title = self.scoped_repo.display_name

    def on_screen_resume(self) -> None:
        generation = self.app.repository_generation
        if generation != self._repository_generation:
            self.on_repositories_refreshed(generation)

    def on_repositories_refreshed(self, generation: int) -> None:
        """Reconcile inbox tabs with one successful repository refresh."""
        self._repository_generation = generation
        self._stop_all_open()
        for tab_id in _TAB_TABLES:
            self._stop_ci_lookups(tab_id)
        self._all_open_cursors.clear()
        self._loaded_tabs.clear()
        scoped_repo_removed = False
        if self.scoped_repo is not None:
            try:
                self.app.services.repository_ref(self.scoped_repo)
            except ServiceError:
                scoped_repo_removed = True
        if not self.app.repos or scoped_repo_removed:
            for table in self.query(MRTable):
                table.clear()
        if not self.scoped_repo and not self.app.repos:
            self.notify("[dim]No forges discovered yet[/]")
        self.action_focus_tab("reviews")

    def action_refresh(self) -> None:
        """Reload the current tab from the forge, past the list cache."""
        tab_id = self.query_one(TabbedContent).active
        self._loaded_tabs.add(tab_id)
        self._load_tab(tab_id, fresh=True)

    def action_go_back(self) -> None:
        if self.scoped_repo:
            self.app.pop_screen()

    def action_quit_or_back(self) -> None:
        if self.scoped_repo:
            self.app.pop_screen()
        else:
            self.app.exit()

    def action_repos_or_back(self) -> None:
        if self.scoped_repo:
            self.app.pop_screen()
        else:
            self.app.push_screen("repo_list")

    def on_tabbed_content_tab_activated(
        self, event: TabbedContent.TabActivated
    ) -> None:
        tab_id = event.pane.id
        if self.query_one(TabbedContent).active != "all-open":
            # Leaving All Open mid-load stops it; coming back loads it again.
            # CI lookups stop too and resume for the rows still waiting. The
            # Reviews and My MRs lookups finish in the background, like the
            # list reads they follow.
            self._stop_all_open()
            self._stop_ci_lookups("all-open")
        elif tab_id == "all-open" and tab_id in self._loaded_tabs:
            self._resume_ci_lookups("all-open")
        if tab_id and tab_id not in self._loaded_tabs:
            self._loaded_tabs.add(tab_id)
            self._load_tab(tab_id)
        self.refresh_bindings()

    def action_focus_tab(self, tab_id: str) -> None:
        tabbed = self.query_one(TabbedContent)
        tabbed.active = tab_id
        if tab_id in self._loaded_tabs:
            return
        self._loaded_tabs.add(tab_id)
        self._load_tab(tab_id)

    def on_unmount(self) -> None:
        for tab_id in _TAB_TABLES:
            self._stop_ci_lookups(tab_id)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "load_more":
            return self._can_load_more()
        return True

    def action_load_more(self) -> None:
        """Read the next page of every All Open repository that has one."""
        if not self._can_load_more():
            return
        self._all_open_pending = True
        self.loading_all_open = True
        self.refresh_bindings()
        self.load_more_all_open(self._all_open_load)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """Load more once the cursor reaches the last All Open row."""
        table = event.data_table
        if table.id != "all-open-table" or table.row_count == 0:
            return
        if event.cursor_row >= table.row_count - 1:
            self.action_load_more()

    def _can_load_more(self) -> bool:
        try:
            active = self.query_one(TabbedContent).active
        except NoMatches:
            return False
        return (
            active == "all-open"
            and "all-open" in self._loaded_tabs
            and not self._all_open_pending
            and bool(self._all_open_cursors)
        )

    def _load_tab(self, tab_id: str, *, fresh: bool = False) -> None:
        if tab_id == "reviews":
            self.load_reviews(fresh=fresh)
        elif tab_id == "my-mrs":
            self.load_my_mrs(fresh=fresh)
        elif tab_id == "all-open":
            self._all_open_load += 1
            self._all_open_pending = True
            self.load_all_open(fresh=fresh, load=self._all_open_load)

    def action_open_in_browser(self) -> None:
        table = self._active_table()
        if table:
            mr = table.get_selected_mr()
            if mr:
                self.app.open_url(mr.web_url)
            else:
                self.notify("No MR selected")

    def action_select_mr(self) -> None:
        table = self._active_table()
        if table:
            mr = table.get_selected_mr()
            if mr:
                from tongs.views.mr_detail import MRDetailScreen

                self.app.push_screen(MRDetailScreen(mr))

    @work(exclusive=True, group="reviews")
    async def load_reviews(self, fresh: bool = False) -> None:
        self.loading_reviews = True
        table = self.query_one("#reviews-table", MRTable)
        table.loading = True
        try:
            if not self.scoped_repo and not self.app.repos:
                table.clear()
                return
            await self._load_page(
                table, "reviews", ReviewScope.MY_REVIEWS, "Reviews", fresh=fresh
            )
        finally:
            self.loading_reviews = False
            with suppress(NoScreen):
                table.loading = False

    @work(exclusive=True, group="my-mrs")
    async def load_my_mrs(self, fresh: bool = False) -> None:
        self.loading_my_mrs = True
        table = self.query_one("#my-mrs-table", MRTable)
        table.loading = True
        try:
            if not self.scoped_repo and not self.app.repos:
                table.clear()
                return
            await self._load_page(
                table, "my-mrs", ReviewScope.MY_MRS, "My MRs", fresh=fresh
            )
        finally:
            self.loading_my_mrs = False
            with suppress(NoScreen):
                table.loading = False

    @work(exclusive=True, group="all-open")
    async def load_all_open(self, fresh: bool = False, load: int | None = None) -> None:
        if load is None:
            self._all_open_load += 1
            load = self._all_open_load
            self._all_open_pending = True
        elif load != self._all_open_load:
            return
        self.loading_all_open = True
        table = self.query_one("#all-open-table", MRTable)
        status = self.query_one("#all-open-status", Static)
        table.loading = True
        status.update("")
        # A new load replaces the rows, so lookups for the old ones stop.
        self._stop_ci_lookups("all-open")
        self._all_open_cursors.clear()
        try:
            if not self.scoped_repo and not self.app.repos:
                table.clear()
                return
            await self._stream_all_open(table, status, load, fresh=fresh)
        finally:
            if load == self._all_open_load:
                self._all_open_pending = False
                self.loading_all_open = False
                with suppress(NoScreen):
                    table.loading = False
                    self.refresh_bindings()

    @work(exclusive=True, group="all-open")
    async def load_more_all_open(self, load: int) -> None:
        """Read the next page of the repositories that have one."""
        if load != self._all_open_load:
            return
        table = self.query_one("#all-open-table", MRTable)
        status = self.query_one("#all-open-status", Static)
        cursors = dict(self._all_open_cursors)
        items: list[ReviewListItem] = []
        failures: list[HostFailure] = []
        status.update(f"[dim]{_loading_more(len(cursors))}[/]")
        try:
            async with aclosing(
                self.app.services.stream_all_open_more(cursors)
            ) as steps:
                async for step in steps:
                    if load != self._all_open_load:
                        return
                    self._apply_all_open_step(table, step, replace=False, fresh=False)
                    items.extend(step.page.items)
                    failures.extend(step.page.failures)
                    if step.completed < step.total:
                        status.update(
                            f"[dim]{_loading_more(step.total - step.completed)}[/]"
                        )
        except ServiceError as error:
            if load == self._all_open_load:
                self.notify(
                    f"All Open: {error.message}", severity="warning", markup=False
                )
        finally:
            if load == self._all_open_load:
                self._all_open_pending = False
                self.loading_all_open = False
                with suppress(NoScreen):
                    status.update(f"[dim]{self._all_open_summary(table)}[/]")
                    self.refresh_bindings()
        if load == self._all_open_load:
            self._notify_failures(
                "All Open", items, self.app.services.deduplicate_failures(failures)
            )

    async def _stream_all_open(
        self, table: MRTable, status: Static, load: int, *, fresh: bool
    ) -> None:
        """Show each repository's rows as soon as that repository finishes."""
        items: list[ReviewListItem] = []
        failures: list[HostFailure] = []
        total = 0
        stream = (
            self.app.services.stream_all_open(fresh=fresh, repository=self.scoped_repo)
            if self.scoped_repo is not None
            else self.app.services.stream_all_open(fresh=fresh)
        )
        try:
            async with aclosing(stream) as steps:
                async for step in steps:
                    if load != self._all_open_load:
                        return
                    total = step.total
                    self._all_open_total = total
                    # The previous rows stay until the first new ones arrive.
                    self._apply_all_open_step(
                        table, step, replace=not items, fresh=fresh
                    )
                    if step.page.items:
                        table.loading = False
                        items.extend(step.page.items)
                    failures.extend(step.page.failures)
                    if step.completed < step.total:
                        status.update(
                            f"[dim]Loading {step.completed} of {step.total} "
                            f"{_repositories(step.total)}[/]"
                        )
        except ServiceError as error:
            if load == self._all_open_load:
                # Rows that already arrived replaced the earlier list, so say
                # the list is incomplete rather than clearing the status.
                status.update(
                    f"[dim]List incomplete: {_open_count(len(items))} loaded[/]"
                    if items
                    else ""
                )
                self.notify(
                    f"All Open: {error.message}", severity="warning", markup=False
                )
                self._notify_failures(
                    "All Open", items, self.app.services.deduplicate_failures(failures)
                )
            return
        if load != self._all_open_load:
            return
        if not items and not failures:
            table.clear()
        if not items and failures:
            failed = len(
                {failure.repository or failure.hostname for failure in failures}
            )
            unloaded = f"{failed} of {total} {_repositories(total)} could not be loaded"
            # Nothing new arrived, so any rows on screen are the earlier load's.
            status.update(
                f"[dim]Showing earlier results: {unloaded}[/]"
                if table.row_count
                else f"[dim]{unloaded[0].upper()}{unloaded[1:]}[/]"
            )
            # Earlier rows still waiting for CI get their lookups again.
            self._queue_ci_lookups("all-open", table, table.pending_ci_mrs())
        else:
            status.update(f"[dim]{self._all_open_summary(table)}[/]")
        self._notify_failures(
            "All Open", items, self.app.services.deduplicate_failures(failures)
        )

    def _apply_all_open_step(
        self,
        table: MRTable,
        step: TUIRepositoryProgress,
        *,
        replace: bool,
        fresh: bool,
    ) -> None:
        """Show one repository page, keep its cursor, and queue its CI lookups.

        ``fresh`` marks rows from an explicit refresh, whose lookups read past
        the CI cache.
        """
        if step.repository is not None:
            if step.page.next_cursor is not None:
                self._all_open_cursors[step.repository] = step.page.next_cursor
            else:
                self._all_open_cursors.pop(step.repository, None)
        if not step.page.items:
            return
        summaries = [item.summary for item in step.page.items]
        table.merge_mrs(
            summaries, self.app.config.ascii_mode, replace=replace, ci_pending=True
        )
        self._queue_ci_lookups("all-open", table, summaries, fresh=fresh)

    def _all_open_summary(self, table: MRTable) -> str:
        """Count the loaded rows and say whether more can be loaded."""
        count = table.row_count
        total = self._all_open_total
        loaded = " loaded" if self._all_open_cursors else ""
        text = (
            f"{_open_count(count)}{loaded}"
            if self.scoped_repo is not None
            else f"{_open_count(count)}{loaded} across {total} {_repositories(total)}"
        )
        if self._all_open_cursors:
            text += ", more to load (m)"
        return text

    def _queue_ci_lookups(
        self,
        tab_id: str,
        table: MRTable,
        summaries: Iterable[MRSummary],
        *,
        fresh: bool = False,
    ) -> None:
        """Look up CI for rows that still show the loading marker.

        Lookups run in table order, a few at a time per tab, so a load more or
        a refresh never waits behind every row's lookup. Each request also
        takes a slot of the per-host request limit shared with every other
        read.
        """
        lookups = self._ci[tab_id]
        for summary in summaries:
            key = mr_row_key(summary)
            if key in lookups.queued or not table.is_ci_pending(summary):
                continue
            lookups.queued.add(key)
            lookups.queue.append((summary, fresh))
        limit = max(1, self.app.config.max_parallel)
        while lookups.workers < min(limit, len(lookups.queue)):
            lookups.workers += 1
            self.run_worker(
                self._run_ci_lookups(tab_id, table, lookups.generation),
                group=lookups.group,
                exit_on_error=False,
            )

    async def _run_ci_lookups(
        self, tab_id: str, table: MRTable, generation: int
    ) -> None:
        lookups = self._ci[tab_id]
        try:
            while generation == lookups.generation and lookups.queue:
                summary, fresh = lookups.queue.popleft()
                key = mr_row_key(summary)
                if table.is_ci_pending(summary):
                    try:
                        status = await self.app.services.get_ci_status(
                            summary, fresh=fresh
                        )
                    except ServiceError:
                        status = CIStatus.UNKNOWN
                    if generation != lookups.generation:
                        return
                    table.set_ci_status(summary, status, self.app.config.ascii_mode)
                # The key stays queued until its lookup ends, so a row is never
                # looked up twice at once. A newer summary that replaced the row
                # meanwhile is looked up next.
                lookups.queued.discard(key)
                current = table.get_mr(key)
                if current is not None and table.is_ci_pending(current):
                    self._queue_ci_lookups(tab_id, table, (current,), fresh=fresh)
        finally:
            if generation == lookups.generation:
                lookups.workers -= 1

    def _stop_ci_lookups(self, tab_id: str) -> None:
        """Drop a tab's queued CI lookups and make any in flight land nowhere."""
        lookups = self._ci[tab_id]
        lookups.generation += 1
        lookups.queue.clear()
        lookups.queued.clear()
        lookups.workers = 0
        self.workers.cancel_group(self, lookups.group)

    def _resume_ci_lookups(self, tab_id: str) -> None:
        """Queue lookups again for rows a stop left waiting for CI."""
        with suppress(NoMatches):
            table = self.query_one(_TAB_TABLES[tab_id], MRTable)
            self._queue_ci_lookups(tab_id, table, table.pending_ci_mrs())

    def _stop_all_open(self) -> None:
        """Cancel an All Open load in flight so the tab loads again on return."""
        if not self._all_open_pending:
            return
        self._all_open_load += 1
        self._all_open_pending = False
        self.workers.cancel_group(self, "all-open")
        self._loaded_tabs.discard("all-open")
        self.loading_all_open = False
        with suppress(NoScreen):
            self.query_one("#all-open-table", MRTable).loading = False
            self.query_one("#all-open-status", Static).update("")

    async def _load_page(
        self,
        table: MRTable,
        tab_id: str,
        scope: ReviewScope,
        label: str,
        *,
        fresh: bool = False,
    ) -> None:
        """Show one Reviews or My MRs read, then fill in its CI per row.

        The list arrives without CI, so each row shows the loading marker
        until its own lookup lands. A refresh reads CI past the cache too.
        """
        try:
            page = await self.app.services.list_reviews(
                scope, repository=self.scoped_repo, fresh=fresh
            )
        except ServiceError as error:
            self.notify(f"{label}: {error.message}", severity="warning", markup=False)
            # The earlier rows stay, so any still waiting for CI get lookups.
            self._queue_ci_lookups(tab_id, table, table.pending_ci_mrs())
            return

        if page.items or not page.failures:
            # The new rows replace the old ones, so lookups for those stop.
            self._stop_ci_lookups(tab_id)
            summaries = [item.summary for item in page.items]
            table.merge_mrs(
                summaries, self.app.config.ascii_mode, replace=True, ci_pending=True
            )
            self._queue_ci_lookups(tab_id, table, summaries, fresh=fresh)
        else:
            self._queue_ci_lookups(tab_id, table, table.pending_ci_mrs())
        self._notify_failures(label, page.items, page.failures)

    def _notify_failures(
        self,
        label: str,
        items: Sequence[ReviewListItem],
        failures: Sequence[HostFailure],
    ) -> None:
        if failures:
            # A host or repository that still contributed rows returned a
            # partial page rather than being skipped, so it is reported apart.
            shown = {item.ref.repository for item in items}
            shown_hosts = {repository.hostname for repository in shown}
            partial: list[HostFailure] = []
            skipped: list[HostFailure] = []
            for failure in failures:
                contributed = (
                    failure.repository in shown
                    if failure.repository is not None
                    else failure.hostname in shown_hosts
                )
                (partial if contributed else skipped).append(failure)
            # Failure messages can carry forge text, so show them literally.
            if partial:
                self.notify(
                    f"{label}: {_failure_details(partial)}",
                    severity="warning",
                    markup=False,
                )
            if skipped:
                self.notify(
                    f"{label} skipped: {_failure_details(skipped)}",
                    severity="warning",
                    markup=False,
                )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.action_select_mr()

    def _active_table(self) -> MRTable | None:
        tabbed = self.query_one(TabbedContent)
        table_id = _TAB_TABLES.get(tabbed.active, "#reviews-table")
        return self.query_one(table_id, MRTable)


def _open_count(count: int) -> str:
    return f"{count} open MR{'' if count == 1 else 's'}"


def _repositories(count: int) -> str:
    return "repository" if count == 1 else "repositories"


def _loading_more(count: int) -> str:
    return f"Loading more from {count} {_repositories(count)}"


# Codes that describe the whole host rather than one repository, so every
# repository on that host fails the same way and is reported once.
_HOST_WIDE_CODES = frozenset(
    {
        ServiceErrorCode.AUTHENTICATION_FAILED,
        ServiceErrorCode.RATE_LIMITED,
        ServiceErrorCode.NETWORK_UNAVAILABLE,
        ServiceErrorCode.CONFIGURATION_INVALID,
    }
)


def _failure_details(failures: list[HostFailure]) -> str:
    """Join failures into one notification, one entry per repository or host.

    A failure names its repository when it has one. Repository failures that
    share a host-wide code collapse into one entry for the host with a count.
    """
    groups: dict[tuple[str, ServiceErrorCode | int], list[HostFailure]] = {}
    for index, failure in enumerate(failures):
        if failure.repository is not None and failure.code in _HOST_WIDE_CODES:
            key: tuple[str, ServiceErrorCode | int] = (failure.hostname, failure.code)
        else:
            key = (failure.hostname, index)
        groups.setdefault(key, []).append(failure)
    entries: list[str] = []
    for group in groups.values():
        first = group[0]
        repositories = {failure.repository for failure in group}
        if len(repositories) > 1:
            entries.append(
                f"{first.hostname}: {first.message} ({len(repositories)} repositories)"
            )
        elif first.repository is not None:
            entries.append(
                f"{first.hostname}/{first.repository.project_path}: {first.message}"
            )
        else:
            entries.append(f"{first.hostname}: {first.message}")
    return "; ".join(entries)
