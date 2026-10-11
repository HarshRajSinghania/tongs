"""``[ui] ascii_mode`` draws every terminal screen with ASCII only (#322).

Each test opens screens through the real app with ``ascii_mode = true`` and
checks the exported screenshot: its SVG markup is ASCII, so any other
character in it is one the screen drew. A second check reads the computed
border, outline and keyline of every widget, hidden ones included, so a box
that only shows later (a search field, a dialog state) is covered as well.
"""

from __future__ import annotations

import asyncio
import html
import re
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from rich.segment import Segment
from textual.app import App
from textual.binding import Binding
from textual.command import CommandPalette, SimpleCommand
from textual.pilot import Pilot
from textual.scrollbar import ScrollBar
from textual.widgets import Header, TextArea

from tests.test_tui_diff_context import GITHUB, GITLAB, _build, _open_diff
from tests.test_tui_mr_services import _app, _settle, _wait_until
from tongs.app import TongsApp
from tongs.forges.models import Discussion, InlineComment, User
from tongs.helpers import markdown_lines
from tongs.views.mr_detail import MRDetailScreen
from tongs.views.review_submit import ReviewSubmitScreen
from tongs.widgets.ascii import (
    HEADER_ICON,
    LOADING_TEXT,
    AsciiCommandPalette,
    AsciiScrollBarRender,
    ascii_key_display,
)
from tongs.widgets.comment_editor import CommentEditor
from tongs.widgets.diff_panel import DiffOptionList, DiffPanel
from tongs.widgets.split_diff import DiffViewMode, SplitDiffView

NOW = datetime(2026, 9, 7, tzinfo=UTC)

# Markdown that makes Textual and Rich draw bullets, a quote bar, a rule, a
# table and a heading box: everything they draw with Unicode by default.
MARKDOWN = """# Release notes

Some **bold** text and a [link](https://example.com).

- first
- second
  - nested

- [ ] a task

1. one
2. two

> a quote

---

| name | value |
|------|-------|
| a    | 1     |

```python
x = 1
```
"""

SAFE_EDGES = {"", "none", "hidden", "blank", "ascii"}
_TEXT = re.compile(r"<text\b[^>]*>(.*?)</text>", re.DOTALL)


def _squash(text: str) -> str:
    return "".join(text.split())


def _screenshot(app: App) -> tuple[str, str]:
    """Return the exported SVG and the text it shows, without whitespace.

    The SVG places each run of text on its own, so the gaps between runs are
    not in it; the expected snippets are compared without whitespace too.
    """
    svg = app.export_screenshot()
    # Rich writes every space as a non-breaking space entity.
    text = html.unescape("".join(_TEXT.findall(svg))).replace("\xa0", " ")
    return svg, _squash(text)


def _unicode_borders(app: App) -> list[str]:
    """Name each widget in the screen stack whose edges draw Unicode lines."""
    found: list[str] = []
    for screen in app.screen_stack:
        for widget in screen.walk_children(with_self=True):
            styles = widget.styles
            for edge in ("top", "right", "bottom", "left"):
                for kind in ("border", "outline"):
                    edge_type = getattr(styles, f"{kind}_{edge}")[0]
                    if edge_type not in SAFE_EDGES:
                        found.append(f"{widget!r} {kind}-{edge}: {edge_type}")
            if styles.keyline[0] != "none":
                found.append(f"{widget!r} keyline: {styles.keyline[0]}")
    return found


async def _assert_ascii(app: App, pilot: Pilot, label: str, *expected: str) -> None:
    """Wait for the expected text to be drawn, then check the screen is ASCII."""
    try:
        async with asyncio.timeout(10):
            while not all(_squash(s) in _screenshot(app)[1] for s in expected):
                await pilot.pause()
    except TimeoutError:
        pass  # The assertions below name what is missing.
    svg, text = _screenshot(app)
    glyphs = sorted({char for char in svg if not char.isascii()})
    assert glyphs == [], f"{label}: {' '.join(glyphs)!r} in\n{text}"
    assert _unicode_borders(app) == [], label
    for snippet in expected:
        assert _squash(snippet) in text, f"{label}: {snippet!r} not shown"


async def _until(pilot: Pilot, predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(10):
        while not predicate():
            await pilot.pause()


def _review_app(
    tmp_path: Path, *, ascii_mode: bool = True, title: str | None = None
) -> TongsApp:
    app, forge = _app(tmp_path)
    app.config.ascii_mode = ascii_mode
    if title is not None:
        forge.summary = replace(forge.summary, title=title)
        forge.detail = replace(forge.detail, title=title)
    forge.detail = replace(forge.detail, description=MARKDOWN)
    root = InlineComment("comment-1", User("bob"), MARKDOWN, NOW, "widget.py", None, 1)
    forge.discussion = Discussion("discussion-1", True, root)

    async def long_log(repo_path: str, job_id: int) -> str:
        return "\n".join(f"log line {number}" for number in range(400))

    forge.get_job_log = long_log  # type: ignore[method-assign]
    return app


async def _open_review(app: TongsApp, pilot: Pilot) -> MRDetailScreen:
    await _settle(app)
    app.screen.query_one("#reviews-table").focus()
    await pilot.press("enter")
    await _wait_until(
        app,
        lambda: (
            isinstance(app.screen, MRDetailScreen)
            and cast(MRDetailScreen, app.screen)._current_review_revision is not None
        ),
    )
    await _settle(app)
    await pilot.pause()
    return cast(MRDetailScreen, app.screen)


async def _show(app: TongsApp, pilot: Pilot, *keys: str) -> None:
    for key in keys:
        await pilot.press(key)
    await _settle(app)
    await pilot.pause()


@pytest.mark.asyncio
async def test_every_screen_is_ascii_only(tmp_path: Path) -> None:
    app = _review_app(tmp_path)
    # Wide enough for the split diff.
    async with app.run_test(size=(200, 50), notifications=True) as pilot:
        await _settle(app)
        await pilot.pause()
        await _assert_ascii(
            app, pilot, "inbox", "Service migration", "FAIL", HEADER_ICON
        )
        for key, label in (("2", "my MRs"), ("3", "all open"), ("1", "inbox")):
            await _show(app, pilot, key)
            await _assert_ascii(app, pilot, label)

        await _show(app, pilot, "r", "slash")
        await _assert_ascii(
            app, pilot, "repository list", "acme/widgets", "type to filter"
        )
        await _show(app, pilot, "escape", "escape")

        screen = await _open_review(app, pilot)
        await _assert_ascii(
            app,
            pilot,
            "overview",
            "tongs - !7 Service migration",
            "* first",
            "- nested",
            "| a quote",
            "-----",
        )

        await _show(app, pilot, "2")
        option_list = screen.query_one(DiffOptionList)
        option_list.focus()
        await _show(app, pilot, "right_square_bracket", "d")
        await _assert_ascii(
            app,
            pilot,
            "unified diff with a thread",
            "v Changed files",
            "`--",
            "| a quote",
        )
        panel = screen.query_one(DiffPanel)
        panel.request_mode(DiffViewMode.SPLIT)
        await _until(pilot, lambda: screen.query_one(SplitDiffView).display)
        await _assert_ascii(app, pilot, "split diff with a thread", "* first")
        panel.request_mode(DiffViewMode.UNIFIED)

        await _show(app, pilot, "3")
        await _assert_ascii(app, pilot, "commits", "Commit title")
        await _show(app, pilot, "4")
        await _assert_ascii(app, pilot, "discussions", "* first", "| a quote", "+---")
        await _show(app, pilot, "5")
        await _assert_ascii(app, pilot, "pipelines", "FAIL Pipeline #101", "+---")
        await _show(app, pilot, "enter")
        await _assert_ascii(
            app, pilot, "jobs", "Pipeline #101  FAIL failed", "FAIL test"
        )
        await _show(app, pilot, "enter")
        # The long log scrolls. Partway down, the default scrollbar draws the
        # ends of its thumb with partial blocks.
        log = screen.query_one("#job-log-content")
        assert log.show_vertical_scrollbar
        log.scroll_to(y=7, animate=False)
        await pilot.pause()
        await _assert_ascii(
            app, pilot, "job log", "Job: test  FAIL failed", "log line 7"
        )
        await _show(app, pilot, "slash")
        await _assert_ascii(app, pilot, "log search")
        await _show(app, pilot, "escape")

        await _show(app, pilot, "question_mark")
        await _assert_ascii(app, pilot, "help panel", "enter Open", "down Down")
        await _show(app, pilot, "question_mark")

        app.notify("A warning", severity="warning")
        app.notify("An error", severity="error")
        screen.query_one("#pipeline-panel").loading = True
        await pilot.pause()
        await _assert_ascii(
            app, pilot, "notifications and loading", LOADING_TEXT, "An error"
        )
        screen.query_one("#pipeline-panel").loading = False
        app.clear_notifications()

        await _show(app, pilot, "ctrl+p")
        await _assert_ascii(app, pilot, "command palette", "Search for commands...")
        await _show(app, pilot, "m")
        await _assert_ascii(app, pilot, "command palette results", "Merge")
        await _show(app, pilot, "escape")

        # The system "Theme" command opens a second palette of its own.
        await _show(app, pilot, "ctrl+p", *"theme")
        await _assert_ascii(app, pilot, "theme command", "Change the current theme")
        await _show(app, pilot, "enter")
        await _until(pilot, lambda: isinstance(app.screen, CommandPalette))
        await _assert_ascii(
            app, pilot, "theme picker", "Search for themes...", "textual-dark"
        )
        await _show(app, pilot, "escape")
        assert not isinstance(app.screen, CommandPalette)

        app.search_commands([SimpleCommand("Say hello", lambda: None)])
        await _until(pilot, lambda: isinstance(app.screen, CommandPalette))
        await _assert_ascii(
            app, pilot, "command search", "Search for commands...", "Say hello"
        )
        await _show(app, pilot, "escape")

        await _show(app, pilot, "1")
        screen.action_review_draft()
        await _wait_until(app, lambda: screen._review_draft is not None, timeout=10)
        screen.action_add_comment()
        editor = screen.query_one("#comment-editor", CommentEditor)
        editor.query_one("#comment-input", TextArea).text = "Looks good"
        await _settle(app)
        await pilot.pause()
        await _assert_ascii(app, pilot, "comment editor", "Looks good")
        editor.action_submit()
        await _wait_until(
            app,
            lambda: (
                screen._review_draft is not None
                and len(screen._review_draft.comments) == 1
            ),
            timeout=10,
        )
        screen.action_review_draft()
        await _until(pilot, lambda: isinstance(app.screen, ReviewSubmitScreen))
        await pilot.pause()
        await _assert_ascii(app, pilot, "review submit dialog", "Looks good", "+---")


@pytest.mark.asyncio
async def test_long_titles_are_clipped_without_an_ellipsis(tmp_path: Path) -> None:
    """At 80 columns a long MR title does not fit in the header."""
    title = "Migrate the widget service to the new storage layer " * 2
    app = _review_app(tmp_path, title=title.strip())
    async with app.run_test(size=(80, 24), notifications=True) as pilot:
        await _settle(app)
        await pilot.pause()
        await _assert_ascii(app, pilot, "inbox at 80 columns", "Migrate the")
        await _open_review(app, pilot)
        await _assert_ascii(
            app, pilot, "overview at 80 columns", "tongs - !7 Migrate the widget"
        )


@pytest.mark.parametrize("host", [GITHUB, GITLAB], ids=["github", "gitlab"])
@pytest.mark.asyncio
async def test_diff_context_rows_are_ascii_only(tmp_path: Path, host) -> None:
    """The expandable gaps, expanded lines and whole file of #381."""
    app, _forge, http = await _build(tmp_path, host)
    app.config.ascii_mode = True
    async with http, app.run_test(size=(200, 60), notifications=True) as pilot:
        panel = await _open_diff(app, pilot)
        option_list = app.screen.query_one(DiffOptionList)
        assert option_list._gap_rows
        await _assert_ascii(app, pilot, "unified gaps", "widget notes.txt")

        option_list.highlighted = sorted(option_list._gap_rows)[1]
        await _show(app, pilot, "e")
        await _assert_ascii(app, pilot, "unified expanded")
        panel.request_mode(DiffViewMode.SPLIT)
        await _settle(app)
        await pilot.pause()
        await _assert_ascii(app, pilot, "split expanded")
        await _show(app, pilot, "F")
        await _assert_ascii(app, pilot, "split full file")
        panel.request_mode(DiffViewMode.UNIFIED)
        await _settle(app)
        await pilot.pause()
        await _assert_ascii(app, pilot, "unified full file")


@pytest.mark.asyncio
async def test_without_ascii_mode_the_screens_keep_their_glyphs(
    tmp_path: Path,
) -> None:
    """The check above can fail: the default look draws Unicode."""
    app = _review_app(tmp_path, ascii_mode=False)
    renderer = ScrollBar.renderer
    async with app.run_test(size=(120, 40)) as pilot:
        await _settle(app)
        await pilot.pause()
        svg, _text = _screenshot(app)
        assert "⭘" in svg  # Textual's header icon
        assert not app.has_class("-ascii")
        assert ScrollBar.renderer is renderer
        await _open_review(app, pilot)
        svg, _text = _screenshot(app)
        assert "•" in svg  # Markdown bullet
        assert _unicode_borders(app) != []
        app.search_themes()
        await _until(pilot, lambda: isinstance(app.screen, CommandPalette))
        assert not isinstance(app.screen, AsciiCommandPalette)
        await _show(app, pilot, "escape")
        await _until(pilot, lambda: not isinstance(app.screen, CommandPalette))
        app.search_commands([SimpleCommand("Say hello", lambda: None)])
        await _until(pilot, lambda: isinstance(app.screen, CommandPalette))
        assert not isinstance(app.screen, AsciiCommandPalette)
        await _show(app, pilot, "escape")


@pytest.mark.asyncio
async def test_scrollbar_renderer_is_restored_when_the_app_exits(
    tmp_path: Path,
) -> None:
    app = _review_app(tmp_path)
    renderer = ScrollBar.renderer
    async with app.run_test(size=(120, 40)):
        await _settle(app)
        assert app.has_class("-ascii")
        assert ScrollBar.renderer is AsciiScrollBarRender
        assert app.screen.query_one(Header).icon == HEADER_ICON
    assert ScrollBar.renderer is renderer


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("enter", "enter"),
        ("down", "down"),
        ("left", "left"),
        ("backspace", "backspace"),
        ("ctrl+up", "^up"),
        ("shift+right", "shift+right"),
    ],
)
def test_ascii_key_display(key: str, expected: str) -> None:
    assert ascii_key_display(Binding(key, "noop")) == expected


@pytest.mark.asyncio
async def test_key_display_and_title_follow_ascii_mode(tmp_path: Path) -> None:
    app = _review_app(tmp_path)
    async with app.run_test():
        assert app.get_key_display(Binding("enter", "noop")) == "enter"
        assert app.get_key_display(Binding("ctrl+r", "noop")) == "^r"
        assert app.get_key_display(Binding("r", "noop", key_display="R")) == "R"
        assert app.format_title("tongs", "!7").plain == "tongs - !7"
        assert app.format_title("tongs", "").plain == "tongs"
        app.config.ascii_mode = False
        assert app.get_key_display(Binding("enter", "noop")) == "⏎"
        assert app.format_title("tongs", "!7").plain == "tongs \u2014 !7"


def _plain(lines: list[list[Segment]]) -> str:
    return "\n".join("".join(segment.text for segment in line) for line in lines)


def test_markdown_lines_draw_ascii_boxes_bullets_and_quotes() -> None:
    plain = _plain(markdown_lines(MARKDOWN, 60, ascii_mode=True))
    assert plain.isascii()
    assert " * first" in plain
    assert "| a quote" in plain
    assert "-" * 20 in plain

    default = _plain(markdown_lines(MARKDOWN, 60))
    assert "• first" in default
    assert "▌ a quote" in default


def test_markdown_lines_clip_wide_table_cells_without_an_ellipsis() -> None:
    table = (
        "| name | detail |\n| --- | --- |\n| " + "x" * 60 + " | " + "y" * 60 + " |\n"
    )
    assert "…" in _plain(markdown_lines(table, 30))
    plain = _plain(markdown_lines(table, 30, ascii_mode=True))
    assert plain.isascii()
    assert "..." in plain


def test_markdown_lines_colour_fenced_code_with_the_code_theme() -> None:
    body = "```python\ndef f():\n    return 1\n```\n"

    def styles(theme: str) -> set[str]:
        return {
            str(segment.style)
            for line in markdown_lines(body, 40, code_theme=theme)
            for segment in line
            if "def" in segment.text
        }

    assert styles("monokai") != styles("friendly")
