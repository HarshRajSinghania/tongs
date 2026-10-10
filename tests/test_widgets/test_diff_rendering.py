"""Diff rendering: word diff whitespace, row backgrounds, highlight caching."""

from __future__ import annotations

from uuid import uuid4

import pytest
from rich.color import Color
from rich.syntax import Syntax
from rich.text import Text
from textual.app import App, ComposeResult
from textual.strip import Strip
from textual.widgets import OptionList
from textual.widgets.option_list import Option

from tongs.diff.models import DiffFile, DiffHunk, DiffLine, FileStatus, LineType
from tongs.state.drafts import DiffSide
from tongs.widgets.diff_panel import (
    DiffContent,
    DiffOptionList,
    DiffPanel,
    DiffRenderer,
    _build_highlight_map,
)
from tongs.widgets.review_draft import DraftMarker
from tongs.widgets.split_diff import DiffViewMode, SplitDiffColumn
from tongs.widgets.tinted_option_list import (
    ADDITION_BACKGROUND,
    DELETION_BACKGROUND,
    strip_option_index,
)

MONOKAI_BACKGROUND = Color.parse("#272822")


def _line(
    old: int | None, new: int | None, content: str, line_type: LineType
) -> DiffLine:
    return DiffLine(old, new, content, line_type)


def _python_file(path: str = "app.py") -> DiffFile:
    lines = (
        _line(1, 1, "def handler(event):", LineType.CONTEXT),
        _line(2, None, "    return event", LineType.DELETION),
        _line(None, 2, "        return event", LineType.ADDITION),
        _line(3, 3, "value = 1", LineType.CONTEXT),
    )
    return DiffFile(
        old_path=path,
        new_path=path,
        status=FileStatus.MODIFIED,
        hunks=(DiffHunk("@@ -1,3 +1,3 @@", 1, 3, 1, 3, lines),),
        additions=1,
        deletions=1,
        language="python",
    )


def _row_strips(option_list: OptionList) -> dict[int, list[Strip]]:
    """Render every visible row and group the strips by option index."""
    rows: dict[int, list[Strip]] = {}
    for y in range(option_list.size.height):
        strip = option_list.render_line(y)
        index = strip_option_index(strip)
        if index is not None:
            rows.setdefault(index, []).append(strip)
    return rows


def _backgrounds(strip: Strip) -> set[Color | None]:
    return {
        segment.style.bgcolor if segment.style else None
        for segment in strip
        if segment.text
    }


class _PanelApp(App[None]):
    def compose(self) -> ComposeResult:
        yield DiffPanel()


# -- word diff ---------------------------------------------------------------


@pytest.mark.parametrize("language", ["text", "python"])
def test_word_diff_keeps_indentation_and_runs_of_spaces(language: str) -> None:
    old = _line(5, None, "    return  a", LineType.DELETION)
    new = _line(None, 5, "        return  b", LineType.ADDITION)
    renderer = DiffRenderer(language)

    old_text = renderer._render_word_diff_line(old, new, is_old=True)
    new_text = renderer._render_word_diff_line(old, new, is_old=False)

    assert old_text.plain.endswith("-    return  a")
    assert new_text.plain.endswith("+        return  b")


def test_word_diff_keeps_leading_tabs_in_plain_text() -> None:
    old = _line(5, None, "\treturn a", LineType.DELETION)
    new = _line(None, 5, "\t\treturn a", LineType.ADDITION)

    new_text = DiffRenderer("text")._render_word_diff_line(old, new, is_old=False)

    assert new_text.plain.endswith("+\t\treturn a")


def test_word_diff_marks_changed_indentation_and_keeps_syntax_colour() -> None:
    file = _python_file()
    _, old, new, _ = file.hunks[0].lines
    renderer = DiffRenderer("python", highlight_map=_build_highlight_map(file))

    text = renderer._render_word_diff_line(old, new, is_old=False)

    content_start = text.plain.index("+") + 1
    underlined = [
        (span.start - content_start, span.end - content_start)
        for span in text.spans
        if not isinstance(span.style, str) and span.style.underline
    ]
    # Only the eight-space indent changed; "return event" is equal.
    assert underlined == [(0, 8)]
    keyword = text.plain.index("return")
    keyword_colours = {
        span.style.color
        for span in text.spans
        if not isinstance(span.style, str)
        and span.style.color is not None
        and span.start <= keyword < span.end
    }
    assert keyword_colours
    assert all(colour.name != "green" for colour in keyword_colours)


def test_word_diff_colours_changes_when_there_is_no_syntax() -> None:
    old = _line(1, None, "alpha beta", LineType.DELETION)
    new = _line(None, 1, "alpha gamma", LineType.ADDITION)

    text = DiffRenderer("text")._render_word_diff_line(old, new, is_old=False)

    gamma = text.plain.index("gamma")
    styles = [
        span.style
        for span in text.spans
        if span.start <= gamma < span.end and not isinstance(span.style, str)
    ]
    assert any(
        style.underline and style.bold and style.color == Color.parse("green")
        for style in styles
    )


# -- token backgrounds -------------------------------------------------------


def test_highlight_map_keeps_colours_and_drops_theme_backgrounds() -> None:
    highlight_map = _build_highlight_map(_python_file())

    texts = [*highlight_map.old.values(), *highlight_map.new.values()]
    styles = [span.style for text in texts for span in text.spans]
    assert any(style.color is not None for style in styles)
    assert all(style.bgcolor is None for style in styles)
    assert all(
        isinstance(text.style, str) or text.style.bgcolor is None for text in texts
    )


def test_line_highlighted_without_a_map_also_drops_backgrounds() -> None:
    text = DiffRenderer("python")._highlight_content("return 1")

    assert text.plain == "return 1"
    assert any(span.style.color is not None for span in text.spans)
    assert all(span.style.bgcolor is None for span in text.spans)


@pytest.mark.asyncio
async def test_unified_added_and_cursor_rows_show_their_backgrounds() -> None:
    app = _PanelApp()
    file = _python_file()
    _, deleted, added, _ = file.hunks[0].lines

    async with app.run_test(size=(120, 20)) as pilot:
        app.query_one(DiffPanel).set_files([file])
        await pilot.pause()
        option_list = app.query_one(DiffOptionList)
        index_of = {id(line): index for index, line in option_list._line_map.items()}
        option_list.highlighted = index_of[id(deleted)]
        await pilot.pause()

        rows = _row_strips(option_list)
        cursor_bg = option_list.get_visual_style(
            "option-list--option", "option-list--option-highlighted"
        ).rich_style.bgcolor

        added_rows = rows[index_of[id(added)]]
        cursor_rows = rows[index_of[id(deleted)]]
        assert "return event" in added_rows[0].text
        assert _backgrounds(added_rows[0]) == {ADDITION_BACKGROUND}
        assert "return event" in cursor_rows[0].text
        assert _backgrounds(cursor_rows[0]) == {cursor_bg}
        assert cursor_bg not in {None, MONOKAI_BACKGROUND, DELETION_BACKGROUND}


@pytest.mark.asyncio
async def test_split_added_and_cursor_rows_show_their_backgrounds() -> None:
    app = _PanelApp()
    file = _python_file()
    context, deleted, added, _ = file.hunks[0].lines

    async with app.run_test(size=(160, 20)) as pilot:
        panel = app.query_one(DiffPanel)
        panel.set_files([file])
        panel.request_mode(DiffViewMode.SPLIT)
        await pilot.pause()
        old = app.query_one("#split-old", SplitDiffColumn)
        new = app.query_one("#split-new", SplitDiffColumn)
        new_index = {id(line): index for index, line in new._line_map.items()}
        old_index = {id(line): index for index, line in old._line_map.items()}
        old.highlighted = old_index[id(context)]
        await pilot.pause()

        new_rows = _row_strips(new)
        old_rows = _row_strips(old)
        cursor_bg = old.get_visual_style(
            "option-list--option", "option-list--option-highlighted"
        ).rich_style.bgcolor

        assert "return event" in new_rows[new_index[id(added)]][0].text
        assert _backgrounds(new_rows[new_index[id(added)]][0]) == {ADDITION_BACKGROUND}
        assert _backgrounds(old_rows[old_index[id(deleted)]][0]) == {
            DELETION_BACKGROUND
        }
        cursor_row = old_rows[old_index[id(context)]][0]
        assert "def handler" in cursor_row.text
        assert _backgrounds(cursor_row) == {cursor_bg}
        assert cursor_bg not in {None, MONOKAI_BACKGROUND}


class _PlainListApp(App[None]):
    def compose(self) -> ComposeResult:
        yield OptionList(Option("first"), Option("second"), Option("third"))


@pytest.mark.asyncio
async def test_textual_option_rows_carry_the_option_index_tints_rely_on() -> None:
    """Render smoke test for the one Textual detail the tinted lists use.

    If a Textual release stops stamping rows with the ``option`` meta, no
    diff row would be tinted; raise the Textual bound only with this green.
    """
    app = _PlainListApp()
    async with app.run_test(size=(40, 6)) as pilot:
        await pilot.pause()
        option_list = app.query_one(OptionList)
        indices = [
            strip_option_index(option_list.render_line(y))
            for y in range(option_list.size.height)
        ]
    assert indices[:3] == [0, 1, 2]
    assert all(index is None for index in indices[3:])


# -- lexing ------------------------------------------------------------------


def test_a_deleted_triple_quote_does_not_colour_new_lines() -> None:
    lines = (
        _line(1, 1, "first = 1", LineType.CONTEXT),
        _line(2, None, 'doc = """', LineType.DELETION),
        _line(None, 2, "second = 2", LineType.ADDITION),
        _line(3, 3, "third = 3", LineType.CONTEXT),
    )
    file = DiffFile(
        "m.py",
        "m.py",
        FileStatus.MODIFIED,
        (DiffHunk("@@ -1,3 +1,3 @@", 1, 3, 1, 3, lines),),
        language="python",
    )
    highlight_map = _build_highlight_map(file)
    _, deleted, added, after = lines

    deleted_text = highlight_map.get(deleted)
    assert deleted_text is not None
    quote = deleted_text.plain.index('"""')
    string_colour = next(
        span.style.color for span in deleted_text.spans if span.start == quote
    )
    for line in (added, after):
        text = highlight_map.get(line, DiffSide.NEW)
        assert text is not None
        assert text.plain == line.content
        assert string_colour not in {span.style.color for span in text.spans}


def test_a_string_opened_in_one_hunk_does_not_colour_the_next() -> None:
    first = (_line(None, 1, 'doc = """', LineType.ADDITION),)
    second = (_line(None, 40, "value = 2", LineType.ADDITION),)
    file = DiffFile(
        "m.py",
        "m.py",
        FileStatus.MODIFIED,
        (
            DiffHunk("@@ -0,0 +1 @@", 0, 0, 1, 1, first),
            DiffHunk("@@ -39,0 +40 @@", 39, 0, 40, 1, second),
        ),
        language="python",
    )
    highlight_map = _build_highlight_map(file)
    opened = highlight_map.get(first[0])
    later = highlight_map.get(second[0])
    assert opened is not None and later is not None

    quote = opened.plain.index('"""')
    string_colour = next(
        span.style.color for span in opened.spans if span.start == quote
    )
    assert string_colour not in {span.style.color for span in later.spans}


def test_a_carriage_return_does_not_shift_colours_onto_other_lines() -> None:
    lines = (
        _line(None, 1, "first = 1\r", LineType.ADDITION),
        _line(None, 2, "second = 2", LineType.ADDITION),
    )
    file = DiffFile(
        "m.py",
        "m.py",
        FileStatus.MODIFIED,
        (DiffHunk("@@ -0,0 +1,2 @@", 0, 0, 1, 2, lines),),
        language="python",
    )

    highlight_map = _build_highlight_map(file)

    assert [highlight_map.get(line).plain.strip() for line in lines] == [
        "first = 1",
        "second = 2",
    ]


# -- highlight caching -------------------------------------------------------


@pytest.fixture
def lexer_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every Pygments run made through Rich's Syntax."""
    calls: list[str] = []
    original = Syntax.highlight

    def counting(self: Syntax, code: str, *args: object, **kwargs: object) -> Text:
        calls.append(code)
        return original(self, code, *args, **kwargs)

    monkeypatch.setattr(Syntax, "highlight", counting)
    return calls


def _marker(file: DiffFile, line: DiffLine, body: str) -> DraftMarker:
    return DraftMarker(
        uuid4(),
        body,
        file.old_path,
        file.new_path,
        line.old_lineno,
        line.new_lineno,
        DiffSide.NEW,
        False,
    )


@pytest.mark.asyncio
async def test_unchanged_draft_markers_do_not_rerun_pygments(
    lexer_calls: list[str],
) -> None:
    app = _PanelApp()
    file = _python_file()
    added = file.hunks[0].lines[2]
    marker = _marker(file, added, "Indent this on purpose?")

    async with app.run_test(size=(120, 20)) as pilot:
        panel = app.query_one(DiffPanel)
        panel.set_files([file])
        panel.set_draft_markers((marker,))
        await pilot.pause()
        lexed = len(lexer_calls)
        assert lexed > 0
        content = app.query_one(DiffContent)
        option_list = app.query_one(DiffOptionList)
        options_before = [
            option_list.get_option_at_index(index)
            for index in range(option_list.option_count)
        ]

        panel.set_draft_markers((marker,))
        await pilot.pause()

        assert len(lexer_calls) == lexed
        assert [
            option_list.get_option_at_index(index)
            for index in range(option_list.option_count)
        ] == options_before

        # A changed marker redraws the file from the cached highlight map.
        panel.set_draft_markers((marker, _marker(file, added, "And this")))
        await pilot.pause()
        assert len(lexer_calls) == lexed
        assert option_list.option_count == len(options_before) + 1

        # Layout switches reuse the same map.
        content.request_mode(DiffViewMode.SPLIT)
        await pilot.pause()
        content.request_mode(DiffViewMode.UNIFIED)
        await pilot.pause()
        assert len(lexer_calls) == lexed


@pytest.mark.asyncio
async def test_highlight_cache_is_per_file_and_cleared_by_new_files(
    lexer_calls: list[str],
) -> None:
    app = _PanelApp()
    first = _python_file("first.py")
    second = _python_file("second.py")

    async with app.run_test(size=(120, 20)) as pilot:
        panel = app.query_one(DiffPanel)
        panel.set_files([first, second])
        await pilot.pause()
        after_first = len(lexer_calls)

        panel.action_next_file()
        await pilot.pause()
        after_second = len(lexer_calls)
        assert after_second > after_first

        panel.action_prev_file()
        await pilot.pause()
        assert len(lexer_calls) == after_second

        panel.set_files([first, second])
        await pilot.pause()
        assert len(lexer_calls) > after_second

        before_placeholder = len(lexer_calls)
        panel.show_placeholder("Loading diff...")
        panel.set_files([first])
        await pilot.pause()
        assert len(lexer_calls) > before_placeholder


@pytest.mark.asyncio
async def test_showing_a_file_renders_it_once_with_its_markers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _PanelApp()
    first = _python_file("first.py")
    second = _python_file("second.py")
    marker = _marker(second, second.hunks[0].lines[2], "Check this")

    async with app.run_test(size=(120, 20)) as pilot:
        panel = app.query_one(DiffPanel)
        panel.set_files([first, second])
        panel.set_draft_markers((marker,))
        await pilot.pause()
        rendered: list[DiffFile] = []
        original = DiffContent._render_current_file

        def spy(self: DiffContent, *args: object, **kwargs: object) -> None:
            rendered.append(self._current_file)
            original(self, *args, **kwargs)

        monkeypatch.setattr(DiffContent, "_render_current_file", spy)
        panel.action_next_file()
        await pilot.pause()

        assert rendered == [second]
        option_list = app.query_one(DiffOptionList)
        prompts = [
            str(option_list.get_option_at_index(index).prompt)
            for index in range(option_list.option_count)
        ]
        assert any("Check this" in prompt for prompt in prompts)
