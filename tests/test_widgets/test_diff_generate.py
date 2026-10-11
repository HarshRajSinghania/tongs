"""Generating a truncated file's diff locally in the terminal diff panel (#382).

The panel never calls a service: it posts ``GenerateRequested`` and
``GenerateCancelRequested`` and the test answers with ``replace_file`` or
``generation_failed`` as the screen would.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from textual.app import App, ComposeResult

from tests.test_diff.test_context import FIXTURES
from tests.test_widgets.test_diff_context_split import WIDE_CONTEXT
from tongs.diff.context import GapPosition
from tongs.diff.generate import generate_file_diff
from tongs.diff.models import DiffFile, DiffLine, FileStatus, LineType
from tongs.diff.parser import parse_diff
from tongs.services.review_files import (
    GENERATE_BINARY_MESSAGE,
    NO_MERGE_BASE_MESSAGE,
    decode_file_text,
    generate_too_large_message,
)
from tongs.widgets.diff_panel import (
    GENERATED_LABEL,
    GENERATION_CANCELLED_MESSAGE,
    GENERATION_FAILED_MESSAGE,
    CommentRequested,
    ContextRequested,
    DiffOptionList,
    DiffPanel,
    GenerateCancelRequested,
    GenerateRequested,
    ReplyRequested,
    generating_notice,
)
from tongs.widgets.split_diff import (
    GENERATED_COMMENT_MESSAGE,
    TRUNCATED_NOTICE,
    DiffViewMode,
    SplitDiffColumn,
)

OLD_TEXT, NEW_TEXT, HUNKS = FIXTURES["two_hunks"]
OLD = decode_file_text(OLD_TEXT.encode())
NEW = decode_file_text(NEW_TEXT.encode())
PATH = "big.txt"

GENERATING = "Generating diff locally (downloading 2 files). Press e again to cancel."


def _withheld(path: str = PATH, status: FileStatus = FileStatus.MODIFIED) -> DiffFile:
    """A file the forge sent with no hunks at all, as GitHub does."""
    return DiffFile(path, path, status, (), additions=2, deletions=2, is_truncated=True)


def _partial() -> DiffFile:
    """A file the forge sent with some hunks but flagged as truncated."""
    (file,) = parse_diff(f"--- a/{PATH}\n+++ b/{PATH}\n{HUNKS}", detect_language=False)
    return replace(file, is_truncated=True)


def _generated(source: DiffFile) -> DiffFile:
    return generate_file_diff(source, OLD, NEW)


class _GenApp(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.generate: list[GenerateRequested] = []
        self.cancels: list[GenerateCancelRequested] = []
        self.context: list[ContextRequested] = []
        self.comments: list[CommentRequested] = []
        self.replies: list[ReplyRequested] = []

    def compose(self) -> ComposeResult:
        yield DiffPanel(id="diff-panel")

    def on_generate_requested(self, event: GenerateRequested) -> None:
        self.generate.append(event)

    def on_generate_cancel_requested(self, event: GenerateCancelRequested) -> None:
        self.cancels.append(event)

    def on_context_requested(self, event: ContextRequested) -> None:
        self.context.append(event)

    def on_comment_requested(self, event: CommentRequested) -> None:
        self.comments.append(event)

    def on_reply_requested(self, event: ReplyRequested) -> None:
        self.replies.append(event)


def _option_list(app: _GenApp) -> DiffOptionList:
    return app.query_one(DiffOptionList)


def _rows(app: _GenApp) -> list[str]:
    option_list = _option_list(app)
    return [
        option_list.get_option_at_index(index).prompt.plain
        for index in range(option_list.option_count)
    ]


def _column_rows(column: SplitDiffColumn) -> list[str]:
    return [
        column.get_option_at_index(index).prompt.plain
        for index in range(column.option_count)
    ]


def _messages(app: _GenApp) -> list[str]:
    return [notification.message for notification in app._notifications]


def _header(app: _GenApp) -> str:
    return str(app.query_one("#diff-file-header").render())


async def _open(app: _GenApp, pilot, *files: DiffFile) -> DiffPanel:
    panel = app.query_one(DiffPanel)
    panel.set_files(list(files))
    _option_list(app).focus()
    await pilot.pause()
    return panel


async def _split(app: _GenApp, pilot, panel: DiffPanel) -> None:
    panel.request_mode(DiffViewMode.SPLIT)
    await pilot.pause()
    app.query_one("#split-new", SplitDiffColumn).focus()
    await pilot.pause()


def _check_numbers(lines: list[DiffLine]) -> None:
    """Every shown line carries the numbers of its text in the two versions."""
    for line in lines:
        if line.line_type in (LineType.CONTEXT, LineType.DELETION, LineType.EXPANDED):
            assert line.old_lineno is not None
            assert OLD.lines[line.old_lineno - 1] == line.content
        if line.line_type in (LineType.CONTEXT, LineType.ADDITION, LineType.EXPANDED):
            assert line.new_lineno is not None
            assert NEW.lines[line.new_lineno - 1] == line.content


def _unified_lines(app: _GenApp) -> list[DiffLine]:
    option_list = _option_list(app)
    rows = {**option_list._line_map, **option_list._readonly_rows}
    return [line for _, line in sorted(rows.items())]


@pytest.mark.asyncio
async def test_withheld_file_shows_the_notice_in_both_layouts() -> None:
    app = _GenApp()
    async with app.run_test(size=(200, 40)) as pilot:
        panel = await _open(app, pilot, _withheld())

        assert _rows(app) == [TRUNCATED_NOTICE]
        assert TRUNCATED_NOTICE == (
            "This file's diff is too large for the forge to send. Press e to "
            "generate it locally (downloads both versions of the file)."
        )
        assert _option_list(app)._gap_rows == {}

        await _split(app, pilot, panel)
        for side in ("#split-old", "#split-new"):
            rows = _column_rows(app.query_one(side, SplitDiffColumn))
            assert rows[1:] == [TRUNCATED_NOTICE]
        assert app.generate == []


@pytest.mark.asyncio
async def test_partial_file_shows_the_notice_above_its_hunks_in_both_layouts() -> None:
    app = _GenApp()
    async with app.run_test(size=(200, 40)) as pilot:
        panel = await _open(app, pilot, _partial())

        rows = _rows(app)
        assert rows[0].strip() == TRUNCATED_NOTICE
        assert rows[1].strip().startswith("@@ -7,7 +7,7 @@")
        # A truncated file has no gap rows: e on it means generate.
        assert _option_list(app)._gap_rows == {}
        assert _option_list(app).get_option_at_index(0).disabled
        assert all(row.isascii() for row in rows)

        await _split(app, pilot, panel)
        for side in ("#split-old", "#split-new"):
            column = app.query_one(side, SplitDiffColumn)
            rows = _column_rows(column)
            assert rows[1].strip() == TRUNCATED_NOTICE
            assert rows[2].strip().startswith("@@ -7,7 +7,7 @@")
            assert column._gap_rows == {}
            assert column.get_option_at_index(1).disabled

        # e on a hunk line of the partial file asks to generate it.
        await pilot.press("e")
        await pilot.pause()
        assert len(app.generate) == 1
        assert app.context == []


@pytest.mark.parametrize("mode", [DiffViewMode.UNIFIED, DiffViewMode.SPLIT])
@pytest.mark.asyncio
async def test_e_on_a_fold_of_a_partial_file_opens_it_and_elsewhere_generates(
    mode: DiffViewMode,
) -> None:
    (wide,) = parse_diff(
        f"--- a/{PATH}\n+++ b/{PATH}\n{WIDE_CONTEXT}", detect_language=False
    )
    file = replace(wide, is_truncated=True)
    app = _GenApp()
    async with app.run_test(size=(200, 40), notifications=True) as pilot:
        panel = await _open(app, pilot, file)
        if mode is DiffViewMode.SPLIT:
            await _split(app, pilot, panel)
        column = (
            app.query_one("#split-new", SplitDiffColumn)
            if mode is DiffViewMode.SPLIT
            else _option_list(app)
        )
        folds = sorted(
            index
            for index, gap in column._gap_rows.items()
            if gap.position is GapPosition.INSIDE
        )
        assert len(folds) == 3

        column.highlighted = folds[1]
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()

        assert app.generate == []
        assert app.context == []
        assert column._line_map[column.highlighted].new_lineno == 14
        remaining = [
            gap
            for gap in column._gap_rows.values()
            if gap.position is GapPosition.INSIDE
        ]
        assert len(remaining) == 2

        # Away from a fold and the lines it showed, e still generates the
        # whole diff.
        column.highlighted = next(
            index for index, line in column._line_map.items() if line.new_lineno == 13
        )
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        assert [event.file is file for event in app.generate] == [True]


@pytest.mark.asyncio
async def test_e_starts_one_generation_and_e_again_cancels_it() -> None:
    app = _GenApp()
    async with app.run_test(size=(200, 40), notifications=True) as pilot:
        file = _withheld()
        panel = await _open(app, pilot, file)

        await pilot.press("e")
        await pilot.pause()
        assert [event.file is file for event in app.generate] == [True]
        assert _rows(app) == [GENERATING]
        assert panel.is_generating(file)

        await pilot.press("e")
        await pilot.pause()
        assert len(app.generate) == 1
        assert [event.file is file for event in app.cancels] == [True]
        assert _rows(app) == [TRUNCATED_NOTICE, GENERATION_CANCELLED_MESSAGE]
        assert not panel.is_generating(file)

        # A reply for the cancelled generation is dropped.
        assert not panel.replace_file(0, _generated(file), NEW)
        assert panel._files[0] is file

        # And e tries again.
        await pilot.press("e")
        await pilot.pause()
        assert len(app.generate) == 2
        assert _rows(app) == [GENERATING]
        assert all(row.isascii() for row in _rows(app))


@pytest.mark.parametrize(
    ("status", "count"),
    [
        (FileStatus.ADDED, "1 file"),
        (FileStatus.DELETED, "1 file"),
        (FileStatus.RENAMED, "2 files"),
    ],
)
def test_generating_notice_counts_the_versions_it_downloads(
    status: FileStatus, count: str
) -> None:
    assert f"(downloading {count})" in generating_notice(_withheld(status=status))


@pytest.mark.parametrize(
    "message",
    [
        generate_too_large_message(),
        GENERATE_BINARY_MESSAGE,
        NO_MERGE_BASE_MESSAGE,
        "Could not generate the diff. (connection reset)",
    ],
)
@pytest.mark.asyncio
async def test_a_failure_restores_the_notice_with_its_reason(message: str) -> None:
    app = _GenApp()
    async with app.run_test(size=(200, 40), notifications=True) as pilot:
        file = _withheld()
        panel = await _open(app, pilot, file)
        await pilot.press("e")
        await pilot.pause()

        panel.generation_failed(file, message)
        await pilot.pause()

        assert _rows(app) == [TRUNCATED_NOTICE, message]
        assert message in _messages(app)
        assert all(row.isascii() for row in _rows(app))
        assert not panel.is_generating(file)

        # A second failure for the same request is ignored.
        panel.generation_failed(file, "late")
        await pilot.pause()
        assert "late" not in _rows(app)


@pytest.mark.asyncio
async def test_a_failure_without_a_reason_still_says_it_failed() -> None:
    app = _GenApp()
    async with app.run_test(size=(200, 40), notifications=True) as pilot:
        file = _withheld()
        panel = await _open(app, pilot, file)
        await pilot.press("e")
        await pilot.pause()
        panel.generation_failed(file, "")
        await pilot.pause()
        assert _rows(app) == [TRUNCATED_NOTICE, GENERATION_FAILED_MESSAGE]


@pytest.mark.parametrize("source", [_withheld(), _partial()], ids=["none", "partial"])
@pytest.mark.asyncio
async def test_generated_diff_replaces_the_file_with_numbers_gaps_and_full_file(
    source: DiffFile,
) -> None:
    app = _GenApp()
    async with app.run_test(size=(200, 60), notifications=True) as pilot:
        panel = await _open(app, pilot, source)
        await pilot.press("e")
        await pilot.pause()

        generated = _generated(source)
        assert panel.replace_file(0, generated, NEW)
        await pilot.pause()

        assert panel._files[0] is generated
        assert GENERATED_LABEL in _header(app)
        rows = _rows(app)
        assert TRUNCATED_NOTICE not in [row.strip() for row in rows]
        assert all(row.isascii() for row in rows)
        _check_numbers(_unified_lines(app))
        changed = [
            line.new_lineno
            for line in _unified_lines(app)
            if line.line_type is LineType.ADDITION
        ]
        assert changed == [10, 20]

        # The new version came with the diff: gaps know their lengths and
        # open without asking for a read.
        option_list = _option_list(app)
        gaps = [rows[index].strip() for index in sorted(option_list._gap_rows)]
        assert gaps == [
            "... 6 unchanged lines (e to expand) ...",
            "... 3 unchanged lines (e to expand) ...",
            "... 7 unchanged lines (e to expand) ...",
        ]
        option_list.highlighted = sorted(option_list._gap_rows)[1]
        await pilot.press("e")
        await pilot.pause()
        assert app.context == []
        assert len(option_list._gap_rows) == 2
        _check_numbers(_unified_lines(app))

        await pilot.press("F")
        await pilot.pause()
        assert app.context == []
        lines = _unified_lines(app)
        assert [line.new_lineno for line in lines if line.new_lineno] == list(
            range(1, len(NEW.lines) + 1)
        )
        _check_numbers(lines)

        # Split view draws the same generated file with both sides numbered.
        await _split(app, pilot, panel)
        for side, version in (("#split-old", OLD), ("#split-new", NEW)):
            column = app.query_one(side, SplitDiffColumn)
            cells = {**column._line_map, **column._readonly_rows}
            numbers = [
                line.old_lineno if side == "#split-old" else line.new_lineno
                for _, line in sorted(cells.items())
            ]
            assert [n for n in numbers if n is not None] == list(
                range(1, len(version.lines) + 1)
            )
        assert app.context == []


@pytest.mark.asyncio
async def test_comments_suggestions_and_replies_on_a_generated_diff_are_refused() -> (
    None
):
    app = _GenApp()
    async with app.run_test(size=(200, 60), notifications=True) as pilot:
        source = _withheld()
        panel = await _open(app, pilot, source)
        await pilot.press("e")
        await pilot.pause()
        panel.replace_file(0, _generated(source), NEW)
        await pilot.pause()
        option_list = _option_list(app)
        option_list.focus()
        line = option_list._line_map[option_list.highlighted]
        assert line.line_type in (LineType.ADDITION, LineType.DELETION)

        for key in ("c", "f3", "r"):
            await pilot.press(key)
            await pilot.pause()
        assert app.comments == []
        assert app.replies == []
        assert _messages(app).count(GENERATED_COMMENT_MESSAGE) == 3

        await _split(app, pilot, panel)
        for key in ("c", "f3", "r"):
            await pilot.press(key)
            await pilot.pause()
        assert app.comments == []
        assert app.replies == []
        assert _messages(app).count(GENERATED_COMMENT_MESSAGE) == 6


@pytest.mark.asyncio
async def test_a_reply_after_new_files_are_loaded_is_dropped() -> None:
    app = _GenApp()
    async with app.run_test(size=(200, 40)) as pilot:
        file = _withheld()
        panel = await _open(app, pilot, file)
        await pilot.press("e")
        await pilot.pause()

        # A refresh loads new file objects for the same paths.
        fresh = _withheld()
        panel.set_files([fresh])
        await pilot.pause()

        assert not panel.replace_file(0, _generated(file), NEW)
        panel.generation_failed(file, "stale")
        panel.drop_generation(file)
        await pilot.pause()
        assert panel._files[0] is fresh
        assert _rows(app) == [TRUNCATED_NOTICE]
        assert GENERATED_LABEL not in _header(app)


@pytest.mark.asyncio
async def test_starting_another_generation_cancels_the_running_one() -> None:
    app = _GenApp()
    async with app.run_test(size=(200, 40)) as pilot:
        first = _withheld("a.txt")
        second = _withheld("b.txt")
        panel = await _open(app, pilot, first, second)
        await pilot.press("e")
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()

        assert [event.file for event in app.generate] == [first, second]
        assert not panel.is_generating(first)
        assert panel.is_generating(second)
        await pilot.press("N")
        await pilot.pause()
        assert _rows(app) == [TRUNCATED_NOTICE, GENERATION_CANCELLED_MESSAGE]


@pytest.mark.asyncio
async def test_a_generated_file_shows_while_another_file_is_on_screen() -> None:
    app = _GenApp()
    async with app.run_test(size=(200, 40)) as pilot:
        source = _withheld("a.txt")
        other = _withheld("b.txt")
        panel = await _open(app, pilot, source, other)
        await pilot.press("e")
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()

        generated = _generated(source)
        assert panel.replace_file(0, generated, NEW)
        await pilot.pause()
        # The file on screen is untouched.
        assert _rows(app) == [TRUNCATED_NOTICE]

        await pilot.press("N")
        await pilot.pause()
        assert GENERATED_LABEL in _header(app)
        _check_numbers(_unified_lines(app))


@pytest.mark.asyncio
async def test_f_on_a_truncated_file_points_to_generation() -> None:
    app = _GenApp()
    async with app.run_test(size=(200, 40), notifications=True) as pilot:
        await _open(app, pilot, _withheld())
        await pilot.press("F")
        await pilot.pause()
        assert (
            "The forge did not send this whole diff. Press e to generate it locally."
            in _messages(app)
        )
        assert app.context == []
        assert app.generate == []


@pytest.mark.asyncio
async def test_e_on_a_binary_truncated_file_does_not_generate() -> None:
    app = _GenApp()
    async with app.run_test(size=(200, 40), notifications=True) as pilot:
        await _open(app, pilot, replace(_withheld(), is_binary=True))
        assert _rows(app) == ["[Binary file]"]
        await pilot.press("e")
        await pilot.pause()
        assert app.generate == []
