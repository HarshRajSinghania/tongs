"""``e`` on an expanded line or an opened fold hides those lines again."""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest
from textual.app import App, ComposeResult

from tests.test_diff.test_context import FIXTURES
from tests.test_widgets.test_diff_context_split import WIDE_CONTEXT
from tongs.diff.context import GapPosition
from tongs.diff.models import DiffFile, LineType
from tongs.diff.parser import parse_diff
from tongs.state.drafts import DiffSide
from tongs.widgets.diff_panel import (
    FULL_FILE_LABEL,
    MOVE_TO_GAP_MESSAGE,
    ContextRequested,
    DiffContent,
    DiffOptionList,
    DiffPanel,
    GenerateCancelRequested,
    GenerateRequested,
)
from tongs.widgets.review_draft import DraftMarker
from tongs.widgets.split_diff import DiffViewMode, SplitDiffColumn

GAP_3 = "... 3 unchanged lines (e to expand) ..."
GAP_6 = "... 6 unchanged lines (e to expand) ..."
FOLD_8 = "... 8 unchanged lines (e to expand) ..."
WIDE_LINES = tuple(
    f"changed {n}" if n in (10, 25) else f"line {n}" for n in range(1, 41)
)


def _split_text(text: str) -> tuple[tuple[str, ...], bool]:
    parts = text.split("\n")
    ends_with_newline = parts[-1] == ""
    if ends_with_newline:
        parts.pop()
    return tuple(parts), ends_with_newline


NEW_LINES, NEW_EOL = _split_text(FIXTURES["two_hunks"][1])


def _file(hunks: str | None = None, path: str = "f.txt") -> DiffFile:
    body = hunks if hunks is not None else FIXTURES["two_hunks"][2]
    (file,) = parse_diff(f"--- a/{path}\n+++ b/{path}\n{body}", detect_language=False)
    return file


class _CollapseApp(App[None]):
    """Records every read and generation the panel asks the screen for."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[ContextRequested] = []
        self.generate: list[GenerateRequested] = []
        self.cancels: list[GenerateCancelRequested] = []

    def compose(self) -> ComposeResult:
        yield DiffPanel(id="diff-panel")

    def on_context_requested(self, event: ContextRequested) -> None:
        self.requests.append(event)

    def on_generate_requested(self, event: GenerateRequested) -> None:
        self.generate.append(event)

    def on_generate_cancel_requested(self, event: GenerateCancelRequested) -> None:
        self.cancels.append(event)


def _option_list(app: _CollapseApp) -> DiffOptionList:
    return app.query_one(DiffOptionList)


def _columns(app: _CollapseApp) -> tuple[SplitDiffColumn, SplitDiffColumn]:
    return (
        app.query_one("#split-old", SplitDiffColumn),
        app.query_one("#split-new", SplitDiffColumn),
    )


def _rows(widget: DiffOptionList | SplitDiffColumn) -> list[str]:
    return [
        widget.get_option_at_index(index).prompt.plain
        for index in range(widget.option_count)
    ]


def _cursor_row(widget: DiffOptionList | SplitDiffColumn) -> str:
    assert widget.highlighted is not None
    return widget.get_option_at_index(widget.highlighted).prompt.plain


def _messages(app: _CollapseApp) -> list[str]:
    return [notification.message for notification in app._notifications]


def _header(app: _CollapseApp) -> str:
    return str(app.query_one("#diff-file-header").render())


def _expanded_numbers(widget: DiffOptionList | SplitDiffColumn) -> list[int]:
    return [
        line.new_lineno
        for _, line in sorted(widget._readonly_rows.items())
        if line.new_lineno is not None
    ]


def _row_of(widget: DiffOptionList | SplitDiffColumn, new_line: int) -> int:
    rows = {**widget._line_map, **widget._readonly_rows}
    return next(i for i, line in sorted(rows.items()) if line.new_lineno == new_line)


async def _open(app: _CollapseApp, pilot, file: DiffFile) -> DiffPanel:
    panel = app.query_one(DiffPanel)
    panel.set_files([file])
    _option_list(app).focus()
    await pilot.pause()
    return panel


async def _open_split(app: _CollapseApp, pilot, file: DiffFile) -> DiffPanel:
    panel = app.query_one(DiffPanel)
    panel.set_files([file])
    panel.request_mode(DiffViewMode.SPLIT)
    await pilot.pause()
    _columns(app)[1].focus()
    await pilot.pause()
    return panel


async def _press_e_at(app: _CollapseApp, pilot, widget, index: int) -> None:
    widget.focus()
    await pilot.pause()
    widget.highlighted = index
    await pilot.pause()
    await pilot.press("e")
    await pilot.pause()


@pytest.mark.asyncio
async def test_e_on_an_expanded_line_collapses_its_gap_and_reexpands_unread() -> None:
    app = _CollapseApp()
    async with app.run_test(size=(100, 60), notifications=True) as pilot:
        file = _file()
        panel = await _open(app, pilot, file)
        option_list = _option_list(app)

        await _press_e_at(app, pilot, option_list, sorted(option_list._gap_rows)[1])
        panel.apply_file_context(file, NEW_LINES, NEW_EOL, 1)
        await pilot.pause()
        assert _expanded_numbers(option_list) == [14, 15, 16]

        # e on the middle expanded line, not only the first one.
        await _press_e_at(app, pilot, option_list, _row_of(option_list, 15))

        assert _expanded_numbers(option_list) == []
        assert _cursor_row(option_list).strip() == GAP_3
        assert [row.strip() for row in _rows(option_list)].count(GAP_3) == 1
        assert len(app.requests) == 1
        assert panel.selection is None

        # The lines stay loaded: e opens the gap again with no new request.
        await pilot.press("e")
        await pilot.pause()
        assert _expanded_numbers(option_list) == [14, 15, 16]
        assert _cursor_row(option_list) == "  14   14    line 14"
        assert len(app.requests) == 1


@pytest.mark.asyncio
async def test_e_on_an_expanded_line_collapses_its_gap_in_split_view() -> None:
    app = _CollapseApp()
    async with app.run_test(size=(200, 60), notifications=True) as pilot:
        file = _file()
        panel = await _open_split(app, pilot, file)
        old, new = _columns(app)

        await _press_e_at(app, pilot, new, min(new._gap_rows))
        panel.apply_file_context(file, NEW_LINES, NEW_EOL, 0)
        await pilot.pause()
        assert _expanded_numbers(new) == [1, 2, 3, 4, 5, 6]

        # Collapse from the old column, on the last expanded line.
        await _press_e_at(app, pilot, old, _row_of(new, 6))

        assert _expanded_numbers(new) == []
        assert _expanded_numbers(old) == []
        assert _cursor_row(old).strip() == GAP_6
        assert _cursor_row(new).strip() == GAP_6
        assert old.highlighted == new.highlighted
        assert len(app.requests) == 1

        await pilot.press("e")
        await pilot.pause()
        assert _expanded_numbers(new) == [1, 2, 3, 4, 5, 6]
        assert len(app.requests) == 1


@pytest.mark.parametrize("mode", [DiffViewMode.UNIFIED, DiffViewMode.SPLIT])
@pytest.mark.asyncio
async def test_e_on_a_line_shown_from_a_fold_folds_it_again(
    mode: DiffViewMode,
) -> None:
    app = _CollapseApp()
    async with app.run_test(size=(200, 60), notifications=True) as pilot:
        file = _file(WIDE_CONTEXT)
        if mode is DiffViewMode.SPLIT:
            await _open_split(app, pilot, file)
            widget = _columns(app)[1]
        else:
            await _open(app, pilot, file)
            widget = _option_list(app)
        folds = sorted(
            index
            for index, gap in widget._gap_rows.items()
            if gap.position is GapPosition.INSIDE
        )
        await _press_e_at(app, pilot, widget, folds[1])
        assert FOLD_8 not in [row.strip() for row in _rows(widget)]

        await _press_e_at(app, pilot, widget, _row_of(widget, 17))

        assert _cursor_row(widget).strip() == FOLD_8
        gap = widget._gap_rows[widget.highlighted]
        assert (gap.position, gap.new_start, gap.length) == (
            GapPosition.INSIDE,
            14,
            8,
        )
        assert app.requests == []

        # Context lines the hunk always showed are left alone.
        await _press_e_at(app, pilot, widget, _row_of(widget, 13))
        assert MOVE_TO_GAP_MESSAGE in _messages(app)
        assert _cursor_row(widget).endswith("line 13")
        assert [row.strip() for row in _rows(widget)].count(FOLD_8) == 1

        # A changed line is never collapsible either.
        changed = next(
            index
            for index, line in widget._line_map.items()
            if line.line_type is LineType.ADDITION
        )
        before = _rows(widget)
        await _press_e_at(app, pilot, widget, changed)
        assert _rows(widget) == before
        assert app.requests == []


@pytest.mark.parametrize("mode", [DiffViewMode.UNIFIED, DiffViewMode.SPLIT])
@pytest.mark.asyncio
async def test_e_on_a_line_a_draft_keeps_shown_leaves_the_fold_open(
    mode: DiffViewMode,
) -> None:
    app = _CollapseApp()
    async with app.run_test(size=(200, 60), notifications=True) as pilot:
        file = _file(WIDE_CONTEXT)
        if mode is DiffViewMode.SPLIT:
            panel = await _open_split(app, pilot, file)
            widget = _columns(app)[1]
        else:
            panel = await _open(app, pilot, file)
            widget = _option_list(app)
        content = app.query_one(DiffContent)
        folds = sorted(
            index
            for index, gap in widget._gap_rows.items()
            if gap.position is GapPosition.INSIDE
        )
        await _press_e_at(app, pilot, widget, folds[1])
        opened = content.file_expansion(file)
        assert opened.folds == frozenset({(14, 8)})

        # A draft on line 17, inside the opened fold, keeps it on screen and
        # splits the run so that lines 14 to 16 would not fold either.
        marker = DraftMarker(
            uuid4(), "Draft note", "f.txt", "f.txt", 17, 17, DiffSide.NEW, False
        )
        panel.set_draft_markers((marker,))
        await pilot.pause()
        before = _rows(widget)
        assert FOLD_8 not in [row.strip() for row in before]

        for new_line in (17, 15):
            row = _row_of(widget, new_line)
            await _press_e_at(app, pilot, widget, row)
            assert widget.highlighted == row
            assert _rows(widget) == before
            assert content.file_expansion(file) == opened
        assert MOVE_TO_GAP_MESSAGE in _messages(app)
        assert app.requests == []


@pytest.mark.asyncio
async def test_e_in_the_whole_file_view_turns_it_off_and_collapses_one_gap() -> None:
    app = _CollapseApp()
    async with app.run_test(size=(200, 60), notifications=True) as pilot:
        file = _file()
        panel = await _open(app, pilot, file)
        option_list = _option_list(app)
        content = app.query_one(DiffContent)

        # The middle gap is open before F.
        await _press_e_at(app, pilot, option_list, sorted(option_list._gap_rows)[1])
        panel.apply_file_context(file, NEW_LINES, NEW_EOL, 1)
        await pilot.pause()
        await pilot.press("F")
        await pilot.pause()
        assert content.file_expansion(file).full
        assert FULL_FILE_LABEL in _header(app)
        assert option_list._gap_rows == {}

        await _press_e_at(app, pilot, option_list, _row_of(option_list, 3))

        assert not content.file_expansion(file).full
        assert FULL_FILE_LABEL not in _header(app)
        assert _cursor_row(option_list).strip() == GAP_6
        # The gap open before F stays open; the one after the hunks closes.
        assert _expanded_numbers(option_list) == [14, 15, 16]
        gap_rows = [
            _rows(option_list)[index].strip() for index in sorted(option_list._gap_rows)
        ]
        assert gap_rows == [GAP_6, "... 7 unchanged lines (e to expand) ..."]
        assert len(app.requests) == 1

        # F on and off again restores exactly that set.
        await pilot.press("F")
        await pilot.pause()
        assert option_list._gap_rows == {}
        await pilot.press("F")
        await pilot.pause()
        assert _expanded_numbers(option_list) == [14, 15, 16]
        assert len(app.requests) == 1


@pytest.mark.parametrize("mode", [DiffViewMode.UNIFIED, DiffViewMode.SPLIT])
@pytest.mark.asyncio
async def test_e_in_the_whole_file_view_folds_a_line_it_showed(
    mode: DiffViewMode,
) -> None:
    app = _CollapseApp()
    async with app.run_test(size=(200, 60), notifications=True) as pilot:
        file = _file(WIDE_CONTEXT)
        if mode is DiffViewMode.SPLIT:
            panel = await _open_split(app, pilot, file)
            widget = _columns(app)[1]
        else:
            panel = await _open(app, pilot, file)
            widget = _option_list(app)
        content = app.query_one(DiffContent)
        await pilot.press("F")
        await pilot.pause()
        panel.apply_file_context(file, WIDE_LINES, True, None)
        await pilot.pause()
        assert content.file_expansion(file).full

        # Line 13 shows with F off too: e there changes nothing.
        await _press_e_at(app, pilot, widget, _row_of(widget, 13))
        assert content.file_expansion(file).full
        assert MOVE_TO_GAP_MESSAGE in _messages(app)

        await _press_e_at(app, pilot, widget, _row_of(widget, 18))
        assert not content.file_expansion(file).full
        assert FULL_FILE_LABEL not in _header(app)
        assert _cursor_row(widget).strip() == FOLD_8
        assert len(app.requests) == 1


@pytest.mark.asyncio
async def test_e_on_a_truncated_file_refolds_before_it_generates() -> None:
    app = _CollapseApp()
    async with app.run_test(size=(200, 60), notifications=True) as pilot:
        file = replace(_file(WIDE_CONTEXT), is_truncated=True)
        await _open(app, pilot, file)
        option_list = _option_list(app)
        folds = sorted(
            index
            for index, gap in option_list._gap_rows.items()
            if gap.position is GapPosition.INSIDE
        )

        await _press_e_at(app, pilot, option_list, folds[1])
        await _press_e_at(app, pilot, option_list, _row_of(option_list, 16))

        assert _cursor_row(option_list).strip() == FOLD_8
        assert app.generate == []

        # Anywhere else e still generates the diff locally.
        await _press_e_at(app, pilot, option_list, _row_of(option_list, 13))
        assert [event.file is file for event in app.generate] == [True]
        assert app.requests == []


@pytest.mark.asyncio
async def test_e_collapses_expanded_lines_of_a_generated_file_with_no_read() -> None:
    app = _CollapseApp()
    async with app.run_test(size=(100, 60), notifications=True) as pilot:
        file = replace(_file(), generated_locally=True)
        await _open(app, pilot, file)
        app.query_one(DiffContent).seed_file_context(file, NEW_LINES, NEW_EOL)
        option_list = _option_list(app)

        await _press_e_at(app, pilot, option_list, sorted(option_list._gap_rows)[1])
        assert _expanded_numbers(option_list) == [14, 15, 16]

        await _press_e_at(app, pilot, option_list, _row_of(option_list, 14))
        assert _expanded_numbers(option_list) == []
        assert _cursor_row(option_list).strip() == GAP_3

        await pilot.press("e")
        await pilot.pause()
        assert _expanded_numbers(option_list) == [14, 15, 16]
        assert app.requests == []
        assert app.generate == []
