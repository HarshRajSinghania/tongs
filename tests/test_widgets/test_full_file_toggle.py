"""F: the whole-file view of the current diff file, and whole-file highlighting."""

from __future__ import annotations

import io

import pytest
from pygments.styles import get_style_by_name
from pygments.token import Token
from rich.console import Console
from textual.app import App, ComposeResult
from textual.widgets._key_panel import BindingsTable

import tongs.widgets.diff_panel as diff_panel_module
from tests.test_diff.test_context import FIXTURES
from tests.test_widgets.test_diff_context_split import WIDE_CONTEXT
from tongs.diff.context import (
    INSIDE_GAP_INDEX,
    FileContextModel,
    FileExpansion,
    GapPosition,
    inside_gap,
)
from tongs.diff.models import DiffFile, FileStatus, LineType
from tongs.diff.parser import parse_diff
from tongs.widgets.diff_panel import (
    CONTEXT_LOADING_MESSAGE,
    FULL_FILE_LABEL,
    ContextRequested,
    DiffOptionList,
    DiffPanel,
)
from tongs.widgets.split_diff import DiffViewMode, SplitDiffColumn

# ``git diff --no-index -U100`` of a 20-line file with its first and last
# lines changed: one hunk covers the file, so nothing lies outside it.
WHOLE_FILE_HUNK = (
    "@@ -1,20 +1,20 @@\n-line 1\n+changed 1\n"
    + "".join(f" line {n}\n" for n in range(2, 20))
    + "-line 20\n+changed 20\n"
)

# A Python file whose docstring spans the unchanged lines above the hunk.
PYTHON_NEW = (
    'x = 1\nDOC = """\n'
    + "".join(f"word{n}\n" for n in range(3, 12))
    + '"""\n'
    + "".join(f"v{n} = {160 if n == 16 else n}\n" for n in range(13, 23))
)
PYTHON_HUNK = (
    "@@ -13,7 +13,7 @@ word11\n v13 = 13\n v14 = 14\n v15 = 15\n-v16 = 16\n"
    "+v16 = 160\n v17 = 17\n v18 = 18\n v19 = 19\n"
)


def _split_text(text: str) -> tuple[tuple[str, ...], bool]:
    parts = text.split("\n")
    ends_with_newline = parts[-1] == ""
    if ends_with_newline:
        parts.pop()
    return tuple(parts), ends_with_newline


NEW_LINES, NEW_EOL = _split_text(FIXTURES["two_hunks"][1])


def _file(
    hunks: str | None = None, path: str = "f.txt", *, language: bool = False
) -> DiffFile:
    body = hunks if hunks is not None else FIXTURES["two_hunks"][2]
    (file,) = parse_diff(
        f"--- a/{path}\n+++ b/{path}\n{body}", detect_language=language
    )
    return file


class _FullApp(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[ContextRequested] = []

    def compose(self) -> ComposeResult:
        yield DiffPanel(id="diff-panel")

    def on_context_requested(self, event: ContextRequested) -> None:
        self.requests.append(event)


def _option_list(app: _FullApp) -> DiffOptionList:
    return app.query_one(DiffOptionList)


def _rows(app: _FullApp) -> list[str]:
    option_list = _option_list(app)
    return [
        option_list.get_option_at_index(index).prompt.plain
        for index in range(option_list.option_count)
    ]


def _new_numbers(app: _FullApp) -> list[int]:
    option_list = _option_list(app)
    lines = {**option_list._line_map, **option_list._readonly_rows}
    return [
        line.new_lineno
        for _, line in sorted(lines.items())
        if line.new_lineno is not None
    ]


def _messages(app: _FullApp) -> list[str]:
    return [notification.message for notification in app._notifications]


def _header(app: _FullApp) -> str:
    return str(app.query_one("#diff-file-header").render())


async def _open(app: _FullApp, pilot, *files: DiffFile) -> DiffPanel:
    panel = app.query_one(DiffPanel)
    panel.set_files(list(files))
    _option_list(app).focus()
    await pilot.pause()
    return panel


async def _full_on(app: _FullApp, pilot, panel: DiffPanel, file: DiffFile) -> None:
    await pilot.press("F")
    await pilot.pause()
    assert app.requests[-1].file is file
    assert app.requests[-1].gap_index is None
    panel.apply_file_context(file, NEW_LINES, NEW_EOL, None)
    await pilot.pause()


@pytest.mark.asyncio
async def test_f_reads_the_file_once_shows_every_line_and_restores_on_off() -> None:
    app = _FullApp()
    async with app.run_test(size=(200, 60), notifications=True) as pilot:
        file = _file()
        panel = await _open(app, pilot, file)
        option_list = _option_list(app)

        # Open the middle gap first, so F off has something to restore.
        option_list.highlighted = sorted(option_list._gap_rows)[1]
        await pilot.press("e")
        panel.apply_file_context(file, NEW_LINES, NEW_EOL, 1)
        await pilot.pause()
        assert len(app.requests) == 1
        before = _rows(app)

        await pilot.press("F")
        await pilot.pause()
        assert len(app.requests) == 1
        assert FULL_FILE_LABEL in _header(app)
        assert option_list._gap_rows == {}
        assert _new_numbers(app) == list(range(1, 31))
        assert all(row.isascii() for row in _rows(app))

        await pilot.press("F")
        await pilot.pause()
        assert FULL_FILE_LABEL not in _header(app)
        assert _rows(app) == before
        assert len(option_list._gap_rows) == 2


@pytest.mark.asyncio
async def test_f_before_any_read_asks_for_the_whole_file() -> None:
    app = _FullApp()
    async with app.run_test(size=(200, 60), notifications=True) as pilot:
        file = _file()
        panel = await _open(app, pilot, file)

        await pilot.press("F")
        await pilot.pause()
        assert [(event.file is file, event.gap_index) for event in app.requests] == [
            (True, None)
        ]
        gap_rows = [_rows(app)[index] for index in sorted(_option_list(app)._gap_rows)]
        assert gap_rows == ["         ... loading ..."] * 3
        await pilot.press("F")
        await pilot.pause()
        assert CONTEXT_LOADING_MESSAGE in _messages(app)
        assert len(app.requests) == 1

        panel.apply_file_context(file, NEW_LINES, NEW_EOL, None)
        await pilot.pause()
        assert FULL_FILE_LABEL in _header(app)
        assert _new_numbers(app) == list(range(1, 31))


@pytest.mark.asyncio
async def test_f_in_split_view_shows_both_sides_in_full() -> None:
    app = _FullApp()
    async with app.run_test(size=(200, 60), notifications=True) as pilot:
        file = _file()
        panel = await _open(app, pilot, file)
        panel.request_mode(DiffViewMode.SPLIT)
        await pilot.pause()
        new = app.query_one("#split-new", SplitDiffColumn)
        old = app.query_one("#split-old", SplitDiffColumn)
        new.focus()
        await pilot.pause()

        await _full_on(app, pilot, panel, file)

        assert new._gap_rows == {} and old._gap_rows == {}
        for column, attribute in ((old, "old_lineno"), (new, "new_lineno")):
            cells = {**column._line_map, **column._readonly_rows}
            numbers = [getattr(line, attribute) for _, line in sorted(cells.items())]
            assert [n for n in numbers if n is not None] == list(range(1, 31))

        await pilot.press("F")
        await pilot.pause()
        assert len(new._gap_rows) == 3


@pytest.mark.asyncio
async def test_f_unfolds_a_hunk_that_covers_the_file_without_reading() -> None:
    app = _FullApp()
    async with app.run_test(size=(200, 60), notifications=True) as pilot:
        await _open(app, pilot, _file(WHOLE_FILE_HUNK))
        option_list = _option_list(app)
        assert [gap.position for gap in option_list._gap_rows.values()] == [
            GapPosition.INSIDE
        ]

        await pilot.press("F")
        await pilot.pause()

        assert app.requests == []
        assert option_list._gap_rows == {}
        assert _new_numbers(app) == list(range(1, 21))
        assert FULL_FILE_LABEL in _header(app)


@pytest.mark.asyncio
async def test_f_opens_in_hunk_folds_too_and_off_closes_them_again() -> None:
    app = _FullApp()
    async with app.run_test(size=(200, 60), notifications=True) as pilot:
        file = _file(WIDE_CONTEXT)
        panel = await _open(app, pilot, file)
        lines = tuple(
            f"changed {n}" if n in (10, 25) else f"line {n}" for n in range(1, 41)
        )

        await pilot.press("F")
        await pilot.pause()
        panel.apply_file_context(file, lines, True, None)
        await pilot.pause()
        assert _option_list(app)._gap_rows == {}
        assert _new_numbers(app) == list(range(1, 41))

        await pilot.press("F")
        await pilot.pause()
        positions = [gap.position for gap in _option_list(app)._gap_rows.values()]
        assert positions.count(GapPosition.INSIDE) == 3


@pytest.mark.parametrize(
    ("file", "message"),
    [
        (
            DiffFile("n.txt", "n.txt", FileStatus.ADDED, _file().hunks),
            "The diff already shows every line of an added file.",
        ),
        (
            DiffFile("d.txt", "d.txt", FileStatus.DELETED, _file().hunks),
            "The diff already shows every line of a deleted file.",
        ),
        (
            DiffFile("b.bin", "b.bin", FileStatus.MODIFIED, (), is_binary=True),
            "Binary file, there are no lines to show.",
        ),
        (
            DiffFile("t.txt", "t.txt", FileStatus.MODIFIED, (), is_truncated=True),
            "The forge did not send this diff. Press o to view it in the browser.",
        ),
        (
            DiffFile("l", "l", FileStatus.MODIFIED, _file().hunks, new_mode="120000"),
            "Symlinks and submodules have no lines to show.",
        ),
    ],
)
@pytest.mark.asyncio
async def test_f_on_a_file_it_cannot_show_says_why(
    file: DiffFile, message: str
) -> None:
    app = _FullApp()
    async with app.run_test(size=(200, 60), notifications=True) as pilot:
        await _open(app, pilot, file)

        await pilot.press("F")
        await pilot.pause()

        assert message in _messages(app)
        assert message.isascii()
        assert app.requests == []
        assert FULL_FILE_LABEL not in _header(app)


@pytest.mark.asyncio
async def test_f_above_the_row_cap_explains_and_keeps_the_gaps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(diff_panel_module, "FULL_FILE_MAX_ROWS", 31)
    app = _FullApp()
    async with app.run_test(size=(200, 60), notifications=True) as pilot:
        file = _file()
        panel = await _open(app, pilot, file)

        await _full_on(app, pilot, panel, file)

        # 30 lines plus the 2 deleted ones.
        assert (
            "File too large to show in full (32 lines). Press e on a gap to expand it."
            in _messages(app)
        )
        assert FULL_FILE_LABEL not in _header(app)
        assert len(_option_list(app)._gap_rows) == 3
        # The read is kept: e opens a gap without another request.
        _option_list(app).highlighted = min(_option_list(app)._gap_rows)
        await pilot.press("e")
        await pilot.pause()
        assert len(app.requests) == 1
        assert len(_option_list(app)._gap_rows) == 2


@pytest.mark.asyncio
async def test_f_resets_when_switching_files() -> None:
    app = _FullApp()
    async with app.run_test(size=(200, 60), notifications=True) as pilot:
        file = _file()
        other = _file(WHOLE_FILE_HUNK, "g.txt")
        panel = await _open(app, pilot, file, other)
        await _full_on(app, pilot, panel, file)
        assert FULL_FILE_LABEL in _header(app)

        await pilot.press("n")
        await pilot.pause()
        assert FULL_FILE_LABEL not in _header(app)
        await pilot.press("N")
        await pilot.pause()

        assert FULL_FILE_LABEL not in _header(app)
        assert len(_option_list(app)._gap_rows) == 3
        # The file read is kept, so F again needs no new request.
        await pilot.press("F")
        await pilot.pause()
        assert len(app.requests) == 1
        assert FULL_FILE_LABEL in _header(app)


@pytest.mark.asyncio
async def test_f_keeps_the_selected_line_and_lands_on_a_gap_it_opens() -> None:
    app = _FullApp()
    async with app.run_test(size=(200, 60), notifications=True) as pilot:
        file = _file()
        panel = await _open(app, pilot, file)
        option_list = _option_list(app)
        selected = panel.selection
        assert selected is not None
        assert selected.line.line_type in (LineType.ADDITION, LineType.DELETION)

        await _full_on(app, pilot, panel, file)
        assert panel.selection == selected
        await pilot.press("F")
        await pilot.pause()
        assert panel.selection == selected

        # From the gap row between the hunks, F lands on its first line.
        option_list.highlighted = sorted(option_list._gap_rows)[1]
        await pilot.press("F")
        await pilot.pause()
        landed = option_list._readonly_rows[option_list.highlighted]
        assert landed.new_lineno == 14
        # And F off goes back to that gap row.
        await pilot.press("F")
        await pilot.pause()
        assert option_list.cursor_gap() is not None
        assert option_list.cursor_gap().new_start == 14


@pytest.mark.asyncio
async def test_help_panel_lists_expand_and_full_file() -> None:
    app = _FullApp()
    async with app.run_test(size=(200, 60), notifications=True) as pilot:
        await _open(app, pilot, _file())
        app.action_show_help_panel()
        await pilot.pause()

        table = app.screen.query_one(BindingsTable).render_bindings_table()
        console = Console(file=io.StringIO(), width=200, color_system=None)
        console.print(table)
        listed = console.file.getvalue()

        assert "Expand" in listed
        assert "Full file" in listed


@pytest.mark.asyncio
async def test_expanded_lines_inside_a_string_take_the_whole_file_colours() -> None:
    string_colour = (
        "#"
        + get_style_by_name("monokai").style_for_token(Token.Literal.String)["color"]
    )
    app = _FullApp()
    async with app.run_test(size=(200, 60), notifications=True) as pilot:
        file = _file(PYTHON_HUNK, "notes.py", language=True)
        assert file.language == "python"
        panel = await _open(app, pilot, file)
        option_list = _option_list(app)
        lines, eol = _split_text(PYTHON_NEW)

        option_list.highlighted = min(option_list._gap_rows)
        await pilot.press("e")
        panel.apply_file_context(file, lines, eol, 0)
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

        index = next(
            index
            for index, line in option_list._readonly_rows.items()
            if line.content == "word4"
        )
        prompt = option_list.get_option_at_index(index).prompt
        start = prompt.plain.index("word4")
        colours = {
            span.style.color.get_truecolor().hex
            for span in prompt.spans
            if span.start <= start < span.end
            and not isinstance(span.style, str)
            and span.style.color is not None
        }
        assert string_colour in colours
        # A hunk line keeps its per-hunk colours.
        assert any(
            line.content == "v16 = 160" for line in option_list._line_map.values()
        )


def test_expansion_keeps_gaps_and_folds_across_the_whole_file_view() -> None:
    file = _file(WIDE_CONTEXT)
    lines = tuple(
        f"changed {n}" if n in (10, 25) else f"line {n}" for n in range(1, 41)
    )
    model = FileContextModel(file, lines, True)
    fold = inside_gap(0, file.hunks[0].lines[13], 8)
    assert fold.index == INSIDE_GAP_INDEX
    assert fold.key == (GapPosition.INSIDE, 14)
    assert fold.covers(21) and not fold.covers(22)

    expansion = FileExpansion().expand(0).reveal_fold(fold)
    hidden = file.hunks[0].lines[13]
    outside = file.hunks[0].lines[2]
    assert expansion.reveals(hidden) and not expansion.reveals(outside)

    full = expansion.toggle_full()
    assert full.reveals(outside) and full.is_shown(1)
    assert full.with_full(False) == expansion
    # 40 lines plus the 2 deleted ones; every expanded line exists up front.
    assert model.full_view_rows() == 42
    assert [line.new_lineno for line in model.all_expanded_lines()] == [
        1,
        *range(34, 41),
    ]
