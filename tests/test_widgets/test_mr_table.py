"""Tests for MR table sort_mrs function."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from textual.app import App, ComposeResult

from tongs.forges.models import (
    CIStatus,
    ForgeHost,
    MRState,
    MRSummary,
    User,
)
from tongs.helpers import forge_label
from tongs.scanner.repo import ForgeType
from tongs.widgets.mr_table import MRTable, mr_row_key, sort_mrs


def _make_mr(
    number: int = 1,
    title: str = "Fix bug",
    username: str = "alice",
    ci_status: CIStatus = CIStatus.SUCCESS,
    updated_at: datetime | None = None,
    created_at: datetime | None = None,
    hostname: str = "gitlab.example.com",
    forge_type: ForgeType = ForgeType.GITLAB,
) -> MRSummary:
    return MRSummary(
        forge_host=ForgeHost(
            hostname=hostname,
            forge_type=forge_type,
            api_base=f"https://{hostname}/api/v4",
        ),
        repo_path="org/repo",
        local_path="/tmp/repo",
        number=number,
        title=title,
        author=User(username=username),
        state=MRState.OPEN,
        is_draft=False,
        source_branch="feature",
        target_branch="main",
        ci_status=ci_status,
        created_at=created_at or datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=updated_at or datetime(2026, 1, 2, tzinfo=UTC),
        web_url="https://gitlab.example.com/org/repo/-/merge_requests/1",
    )


class TestSortMrsByUpdated:
    def test_sorts_by_updated_at_descending(self):
        older = _make_mr(number=1, updated_at=datetime(2026, 1, 1, tzinfo=UTC))
        newer = _make_mr(number=2, updated_at=datetime(2026, 6, 1, tzinfo=UTC))
        result = sort_mrs([older, newer], "updated")
        assert result[0].number == 2
        assert result[1].number == 1

    def test_updated_is_default_sort(self):
        """Any unrecognized key falls back to updated sort."""
        older = _make_mr(number=1, updated_at=datetime(2026, 1, 1, tzinfo=UTC))
        newer = _make_mr(number=2, updated_at=datetime(2026, 6, 1, tzinfo=UTC))
        result = sort_mrs([older, newer], "nonsense")
        assert result[0].number == 2


class TestSortMrsByTitle:
    def test_case_insensitive(self):
        mr_upper = _make_mr(number=1, title="Zebra")
        mr_lower = _make_mr(number=2, title="apple")
        result = sort_mrs([mr_upper, mr_lower], "title")
        assert result[0].title == "apple"
        assert result[1].title == "Zebra"


class TestSortMrsByCi:
    def test_failed_first_success_last(self):
        mr_success = _make_mr(number=1, ci_status=CIStatus.SUCCESS)
        mr_failed = _make_mr(number=2, ci_status=CIStatus.FAILED)
        mr_running = _make_mr(number=3, ci_status=CIStatus.RUNNING)
        result = sort_mrs([mr_success, mr_failed, mr_running], "ci")
        assert result[0].ci_status == CIStatus.FAILED
        assert result[1].ci_status == CIStatus.RUNNING
        assert result[2].ci_status == CIStatus.SUCCESS

    def test_same_ci_breaks_tie_by_title(self):
        mr_b = _make_mr(number=1, title="Beta", ci_status=CIStatus.FAILED)
        mr_a = _make_mr(number=2, title="Alpha", ci_status=CIStatus.FAILED)
        result = sort_mrs([mr_b, mr_a], "ci")
        assert result[0].title == "Alpha"
        assert result[1].title == "Beta"


class TestSortMrsByAuthor:
    def test_sorts_by_username(self):
        # Titles run opposite to usernames, so a title-first key fails.
        mr_charlie = _make_mr(number=1, username="charlie", title="Alpha")
        mr_alice = _make_mr(number=2, username="alice", title="Charlie")
        mr_bob = _make_mr(number=3, username="bob", title="Bravo")
        result = sort_mrs([mr_charlie, mr_alice, mr_bob], "author")
        assert [m.author.username for m in result] == ["alice", "bob", "charlie"]

    def test_same_author_breaks_tie_by_title(self):
        mr_b = _make_mr(number=1, title="Beta", username="alice")
        mr_a = _make_mr(number=2, title="Alpha", username="alice")
        result = sort_mrs([mr_b, mr_a], "author")
        assert result[0].title == "Alpha"
        assert result[1].title == "Beta"


class TestForgeMarker:
    def test_unknown_forge_gets_placeholder(self):
        assert "GH" not in forge_label(None) and "GL" not in forge_label(None)

    @pytest.mark.asyncio
    async def test_inbox_rows_show_gh_and_gl_markers(self):
        github = _make_mr(
            number=1,
            title="From GitHub",
            hostname="github.com",
            forge_type=ForgeType.GITHUB,
        )
        gitlab = _make_mr(number=2, title="From GitLab")

        class TableApp(App):
            def compose(self) -> ComposeResult:
                yield MRTable()

        app = TableApp()
        async with app.run_test() as pilot:
            table = app.query_one(MRTable)
            table.setup_columns()
            table.add_mr_row(github)
            table.add_mr_row(gitlab)
            await pilot.pause()
            first = [str(cell) for cell in table.get_row_at(0)]
            second = [str(cell) for cell in table.get_row_at(1)]
            assert any("GH" in cell for cell in first), first
            assert any("GL" in cell for cell in second), second


class _TableApp(App):
    def compose(self) -> ComposeResult:
        yield MRTable()


def _ci_cell(table: MRTable, mr: MRSummary) -> str:
    return str(table.get_cell(mr_row_key(mr), "ci"))


class TestPendingCI:
    """Rows wait for CI with a loading marker and fill in place (#396)."""

    @pytest.mark.asyncio
    async def test_only_unknown_rows_wait_and_fill_in_place(self):
        waiting = _make_mr(number=1, ci_status=CIStatus.UNKNOWN)
        known = _make_mr(number=2, ci_status=CIStatus.FAILED)
        app = _TableApp()
        async with app.run_test():
            table = app.query_one(MRTable)
            table.setup_columns()
            table.merge_mrs([waiting, known], ci_pending=True)

            assert _ci_cell(table, waiting) == "…"
            assert _ci_cell(table, known) == "●"
            assert table.pending_ci_mrs() == [waiting]

            assert table.set_ci_status(waiting, CIStatus.RUNNING) is True
            assert _ci_cell(table, waiting) == "▶"
            assert table.pending_ci_mrs() == []
            # A second result for the same row changes nothing.
            assert table.set_ci_status(waiting, CIStatus.FAILED) is False
            assert table._mr_data[mr_row_key(waiting)].ci_status is CIStatus.RUNNING

    @pytest.mark.asyncio
    async def test_a_result_for_a_replaced_or_removed_row_is_dropped(self):
        old = _make_mr(number=1, title="Old", ci_status=CIStatus.UNKNOWN)
        new = _make_mr(number=1, title="New", ci_status=CIStatus.UNKNOWN)
        app = _TableApp()
        async with app.run_test():
            table = app.query_one(MRTable)
            table.setup_columns()
            table.merge_mrs([old], ci_pending=True)
            table.merge_mrs([new], ci_pending=True)

            assert table.set_ci_status(old, CIStatus.FAILED) is False
            assert _ci_cell(table, new) == "…"

            table.clear()
            assert table.set_ci_status(new, CIStatus.FAILED) is False
            assert table.row_count == 0

    @pytest.mark.asyncio
    async def test_ascii_marker_and_status(self):
        waiting = _make_mr(number=1, ci_status=CIStatus.UNKNOWN)
        app = _TableApp()
        async with app.run_test():
            table = app.query_one(MRTable)
            table.setup_columns()
            table.merge_mrs([waiting], ascii_mode=True, ci_pending=True)
            assert _ci_cell(table, waiting) == ".."

            table.set_ci_status(waiting, CIStatus.SUCCESS, ascii_mode=True)
            assert _ci_cell(table, waiting) == "OK"

    @pytest.mark.asyncio
    async def test_ci_sort_moves_a_filled_row_and_keeps_the_cursor(self):
        alpha = _make_mr(number=1, title="Alpha", ci_status=CIStatus.UNKNOWN)
        bravo = _make_mr(number=2, title="Bravo", ci_status=CIStatus.UNKNOWN)
        charlie = _make_mr(number=3, title="Charlie", ci_status=CIStatus.UNKNOWN)
        app = _TableApp()
        async with app.run_test(notifications=True) as pilot:
            table = app.query_one(MRTable)
            table.setup_columns()
            table.cursor_type = "row"
            table.merge_mrs([alpha, bravo, charlie], ci_pending=True)
            table.focus()
            await pilot.press("s", "s")
            assert table._sort_key == "ci"
            # Cycling the sort rebuilds the rows but keeps them waiting.
            assert [_ci_cell(table, mr) for mr in (alpha, bravo, charlie)] == [
                "…",
                "…",
                "…",
            ]
            await pilot.press("down")
            assert table.get_selected_mr() == bravo

            table.set_ci_status(charlie, CIStatus.FAILED)

            order = [table._mr_data[row.key.value].number for row in table.ordered_rows]
            assert order == [3, 1, 2]
            assert table.get_selected_mr().number == 2  # type: ignore[union-attr]
            assert table.cursor_row == 2
            for index, row in enumerate(table.ordered_rows):
                assert str(table.get_row_at(index)[2]) == str(
                    table._mr_data[row.key.value].number
                )
