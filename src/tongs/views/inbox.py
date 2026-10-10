"""MR inbox dashboard -- the default screen."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import aclosing, suppress
from typing import ClassVar

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.dom import NoScreen
from textual.reactive import reactive
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static, TabbedContent, TabPane

from tongs.scanner.repo import Repo
from tongs.services.errors import ServiceError, ServiceErrorCode
from tongs.services.models import HostFailure, ReviewListItem, ReviewScope
from tongs.widgets.mr_table import MRTable


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
            self._stop_all_open()
        if tab_id and tab_id not in self._loaded_tabs:
            self._loaded_tabs.add(tab_id)
            self._load_tab(tab_id)

    def action_focus_tab(self, tab_id: str) -> None:
        tabbed = self.query_one(TabbedContent)
        tabbed.active = tab_id
        if tab_id in self._loaded_tabs:
            return
        self._loaded_tabs.add(tab_id)
        self._load_tab(tab_id)

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
            await self._load_page(table, ReviewScope.MY_REVIEWS, "Reviews", fresh=fresh)
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
            await self._load_page(table, ReviewScope.MY_MRS, "My MRs", fresh=fresh)
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
        try:
            if not self.scoped_repo and not self.app.repos:
                table.clear()
                return
            if self.scoped_repo:
                await self._load_page(
                    table, ReviewScope.ALL_OPEN, "All Open", fresh=fresh
                )
                if load == self._all_open_load:
                    status.update(f"[dim]{_open_count(table.row_count)}[/]")
            else:
                await self._stream_all_open(table, status, load, fresh=fresh)
        finally:
            if load == self._all_open_load:
                self._all_open_pending = False
                self.loading_all_open = False
                with suppress(NoScreen):
                    table.loading = False

    async def _stream_all_open(
        self, table: MRTable, status: Static, load: int, *, fresh: bool
    ) -> None:
        """Show each repository's rows as soon as that repository finishes."""
        ascii_mode = self.app.config.ascii_mode
        items: list[ReviewListItem] = []
        failures: list[HostFailure] = []
        total = 0
        try:
            async with aclosing(
                self.app.services.stream_all_open(fresh=fresh)
            ) as steps:
                async for step in steps:
                    if load != self._all_open_load:
                        return
                    total = step.total
                    if step.page.items:
                        # The previous rows stay until the first new ones arrive.
                        table.merge_mrs(
                            (item.summary for item in step.page.items),
                            ascii_mode,
                            replace=not items,
                        )
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
                status.update("")
                self.notify(
                    f"All Open: {error.message}", severity="warning", markup=False
                )
            return
        if load != self._all_open_load:
            return
        if not items and not failures:
            table.clear()
        status.update(
            f"[dim]{_open_count(table.row_count)} across {total} "
            f"{_repositories(total)}[/]"
        )
        self._notify_failures(
            "All Open", items, self.app.services.deduplicate_failures(failures)
        )

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
        self, table: MRTable, scope: ReviewScope, label: str, *, fresh: bool = False
    ) -> None:
        try:
            page = await self.app.services.list_reviews(
                scope, repository=self.scoped_repo, fresh=fresh
            )
        except ServiceError as error:
            self.notify(f"{label}: {error.message}", severity="warning", markup=False)
            return

        if page.items or not page.failures:
            table.clear()
            for item in page.items:
                table.add_mr_row(item.summary, self.app.config.ascii_mode)
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
        tab_map = {
            "reviews": "#reviews-table",
            "my-mrs": "#my-mrs-table",
            "all-open": "#all-open-table",
        }
        table_id = tab_map.get(tabbed.active, "#reviews-table")
        return self.query_one(table_id, MRTable)


def _open_count(count: int) -> str:
    return f"{count} open MR{'' if count == 1 else 's'}"


def _repositories(count: int) -> str:
    return "repository" if count == 1 else "repositories"


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
