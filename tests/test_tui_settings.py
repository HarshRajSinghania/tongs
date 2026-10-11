"""The ``[ui]`` and ``[editor]`` settings change what the terminal does (#145).

Each setting gets a test that shows its effect: the app theme, the diff
syntax style, the starting diff layout, hiding draft MRs, and the external
editor that F2 and F3 start.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from textual.app import App, ComposeResult
from textual.pilot import Pilot
from textual.theme import Theme
from textual.widgets import TextArea

from tests.test_tui_diff_context import _build, _open_diff
from tests.test_tui_session import (
    FixedPluginRegistry,
    MockForgeClient,
    MockForgeRegistry,
    make_app,
    make_repo,
    make_summary,
    settle,
)
from tongs.app import TongsApp
from tongs.config import Config
from tongs.diff.context import FileContextModel
from tongs.diff.models import DiffFile, DiffHunk, DiffLine, FileStatus, LineType
from tongs.forges.models import ForgeHost
from tongs.scanner.repo import ForgeType
from tongs.views.inbox import InboxScreen
from tongs.widgets import external_editor
from tongs.widgets.comment_editor import CommentEditor
from tongs.widgets.diff_panel import (
    DiffContent,
    DiffOptionList,
    DiffPanel,
    DiffRenderer,
    _build_highlight_map,
    _highlight_new_file,
)
from tongs.widgets.discussion_list import render_diff_snippet
from tongs.widgets.external_editor import (
    BAD_COMMAND_MESSAGE,
    EDITOR_DISABLED_MESSAGE,
    NO_EDITOR_MESSAGE,
    WINDOWS_MESSAGE,
    EditorUnavailableError,
    editor_argv,
    find_editor,
)
from tongs.widgets.mr_table import MRTable
from tongs.widgets.pipeline_panel import PipelinePanel
from tongs.widgets.split_diff import DiffViewMode

GITHUB = ForgeHost("github.com", ForgeType.GITHUB, "https://api.github.com")


def _messages(app: App) -> list[str]:
    return [notification.message for notification in app._notifications]


async def _until(pilot: Pilot, predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(5):
        while not predicate():
            await pilot.pause()


# -- [ui] theme -------------------------------------------------------------


def _theme_app(tmp_path: Path, theme: str | None) -> TongsApp:
    registry = MockForgeRegistry({})
    app, _session, _cache = make_app(tmp_path, [], registry)
    if theme is not None:
        app.config.theme = theme
    return app


@pytest.mark.asyncio
async def test_default_theme_keeps_textual_dark(tmp_path: Path) -> None:
    app = _theme_app(tmp_path, None)
    async with app.run_test(notifications=True):
        await settle(app)
        assert app.theme == "textual-dark"
        assert not any("theme" in message for message in _messages(app))


@pytest.mark.asyncio
@pytest.mark.parametrize("theme", ["dracula", "monokai", "textual-light"])
async def test_configured_theme_is_the_app_theme(tmp_path: Path, theme: str) -> None:
    app = _theme_app(tmp_path, theme)
    async with app.run_test(notifications=True):
        await settle(app)
        assert isinstance(app.screen, InboxScreen)
        assert app.theme == theme
        assert not any("theme" in message for message in _messages(app))


@pytest.mark.asyncio
async def test_unknown_theme_falls_back_with_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    app = _theme_app(tmp_path, "no-such-theme")
    with caplog.at_level("WARNING", logger="tongs.app"):
        async with app.run_test(notifications=True):
            await settle(app)
            assert app.theme == "textual-dark"
            warnings = [m for m in _messages(app) if "[ui] theme" in m]
    assert warnings == ["Unknown [ui] theme 'no-such-theme'; using textual-dark."]
    assert [r.getMessage() for r in caplog.records if r.name == "tongs.app"] == (
        warnings
    )


class _ThemePlugins(FixedPluginRegistry):
    """Registers a theme the way a plugin's start-up code could."""

    async def on_app_ready(self, app: object) -> None:
        cast(App, app).register_theme(
            Theme(name="plugin-theme", primary="#336699", dark=True)
        )


@pytest.mark.asyncio
async def test_theme_registered_at_start_up_is_accepted(tmp_path: Path) -> None:
    registry = MockForgeRegistry({})
    app, _session, _cache = make_app(
        tmp_path, [], registry, plugin_registry=_ThemePlugins()
    )
    app.config.theme = "plugin-theme"
    async with app.run_test(notifications=True):
        await settle(app)
        assert app.theme == "plugin-theme"
        assert not any("theme" in message for message in _messages(app))


# -- [ui] syntax_theme ------------------------------------------------------


def _python_file() -> DiffFile:
    lines = (
        DiffLine(1, 1, "def keep(value):", LineType.CONTEXT),
        DiffLine(2, None, "    return None", LineType.DELETION),
        DiffLine(None, 2, "    return value  # changed", LineType.ADDITION),
    )
    return DiffFile(
        old_path="pkg/mod.py",
        new_path="pkg/mod.py",
        status=FileStatus.MODIFIED,
        hunks=(DiffHunk("@@ -1,2 +1,2 @@", 1, 2, 1, 2, lines),),
        additions=1,
        deletions=1,
        language="python",
    )


def _styles(text: object) -> list[str]:
    """The span styles of a highlighted line, as strings."""
    return [str(span.style) for span in getattr(text, "spans", [])]


def _keyword_style(highlight: dict[int, object], line: DiffLine) -> str:
    return _styles(highlight[id(line)])[0]


def test_highlight_map_uses_the_given_style() -> None:
    file = _python_file()
    first = file.hunks[0].lines[0]
    default = _build_highlight_map(file).new
    monokai = _build_highlight_map(file, "monokai").new
    friendly = _build_highlight_map(file, "friendly").new
    assert _keyword_style(default, first) == _keyword_style(monokai, first)
    assert _keyword_style(default, first) != _keyword_style(friendly, first)


def test_whole_file_and_single_line_highlighting_use_the_given_style() -> None:
    hunk_lines = (
        DiffLine(4, 4, "x = 1", LineType.CONTEXT),
        DiffLine(5, None, "y = 2", LineType.DELETION),
        DiffLine(None, 5, "y = 3", LineType.ADDITION),
    )
    file = replace(
        _python_file(), hunks=(DiffHunk("@@ -4,2 +4,2 @@", 4, 2, 4, 2, hunk_lines),)
    )
    lines = ["def keep(value):", "    return 1", "", "x = 1", "y = 3"]
    model = FileContextModel(file, lines, ends_with_newline=True)
    expanded = model.all_expanded_lines()
    assert expanded
    monokai = _highlight_new_file("python", model)
    friendly = _highlight_new_file("python", model, "friendly")
    assert _styles(monokai[id(expanded[0])]) != _styles(friendly[id(expanded[0])])

    content = "def keep(value):"
    plain = DiffRenderer("python")._highlight_content(content)
    styled = DiffRenderer("python", syntax_theme="friendly")._highlight_content(content)
    assert plain.plain == styled.plain == content
    assert _styles(plain) != _styles(styled)


def test_discussion_snippet_uses_the_given_style() -> None:
    file = _python_file()
    default = render_diff_snippet(file, 1, context=0)
    friendly = render_diff_snippet(file, 1, context=0, syntax_theme="friendly")
    assert [line.plain for line in default] == [line.plain for line in friendly]
    assert [_styles(line) for line in default] != [_styles(line) for line in friendly]


class _ConfiguredDiffApp(App[None]):
    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config

    def compose(self) -> ComposeResult:
        yield DiffPanel()


@pytest.mark.asyncio
async def test_diff_view_highlights_with_the_configured_style() -> None:
    file = _python_file()
    added = file.hunks[0].lines[2]
    expected = _styles(_build_highlight_map(file, "friendly").new[id(added)])
    app = _ConfiguredDiffApp(Config(syntax_theme="friendly"))
    async with app.run_test(size=(160, 30)) as pilot:
        panel = app.query_one(DiffPanel)
        panel.set_files([file])
        await pilot.pause()
        content = app.query_one(DiffContent)
        assert _styles(content._file_highlight(file).new[id(added)]) == expected
        option_list = app.query_one(DiffOptionList)
        row = next(
            index for index, line in option_list._line_map.items() if line is added
        )
        prompt = option_list.get_option_at_index(row).prompt
        shown = [str(span.style) for span in prompt.spans]
        assert all(style in shown for style in expected)

        panel.request_mode(DiffViewMode.SPLIT)
        await pilot.pause()
        assert panel.mode_state.effective is DiffViewMode.SPLIT
        assert _styles(content._file_highlight(file).new[id(added)]) == expected


# -- [ui] diff_style --------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("style", "expected"),
    [("unified", DiffViewMode.UNIFIED), ("split", DiffViewMode.SPLIT)],
)
async def test_diff_view_starts_in_the_configured_layout(
    style: str, expected: DiffViewMode
) -> None:
    app = _ConfiguredDiffApp(Config(diff_style=style))
    async with app.run_test(size=(160, 30)) as pilot:
        panel = app.query_one(DiffPanel)
        panel.set_files([_python_file()])
        await pilot.pause()
        assert panel.mode_state.requested is expected
        assert panel.mode_state.effective is expected

        # v still switches for the rest of the view.
        panel.action_toggle_diff_mode()
        await pilot.pause()
        assert panel.mode_state.requested is not expected


@pytest.mark.asyncio
async def test_configured_split_falls_back_to_unified_when_narrow() -> None:
    app = _ConfiguredDiffApp(Config(diff_style="split"))
    async with app.run_test(size=(70, 30)) as pilot:
        panel = app.query_one(DiffPanel)
        panel.set_files([_python_file()])
        await pilot.pause()
        assert panel.mode_state.requested is DiffViewMode.SPLIT
        assert panel.mode_state.effective is DiffViewMode.UNIFIED


@pytest.mark.asyncio
async def test_review_diff_opens_split_when_configured(tmp_path: Path) -> None:
    app, _forge, http = await _build(tmp_path, GITHUB)
    app.config.diff_style = "split"
    async with http, app.run_test(size=(200, 60)) as pilot:
        panel = await _open_diff(app, pilot)
        assert panel.mode_state.requested is DiffViewMode.SPLIT
        assert panel.mode_state.effective is DiffViewMode.SPLIT


# -- [ui] show_draft_mrs ----------------------------------------------------


def _inbox_app(tmp_path: Path, show_drafts: bool) -> TongsApp:
    ready = make_summary(GITHUB, number=7)
    draft = replace(make_summary(GITHUB, number=8), is_draft=True, title="WIP")
    registry = MockForgeRegistry(
        {GITHUB.hostname: MockForgeClient(GITHUB, [ready, draft])}
    )
    app, _session, _cache = make_app(tmp_path, [make_repo(tmp_path)], registry)
    app.config.show_draft_mrs = show_drafts
    return app


def _numbers(table: MRTable) -> list[int]:
    return sorted(mr.number for mr in table._mr_data.values())


@pytest.mark.asyncio
@pytest.mark.parametrize(("show_drafts", "expected"), [(True, [7, 8]), (False, [7])])
async def test_show_draft_mrs_controls_every_inbox_list(
    tmp_path: Path, show_drafts: bool, expected: list[int]
) -> None:
    app = _inbox_app(tmp_path, show_drafts)
    async with app.run_test() as pilot:
        await settle(app)
        inbox = cast(InboxScreen, app.screen)
        assert _numbers(inbox.query_one("#reviews-table", MRTable)) == expected
        await pilot.press("2")
        await settle(app)
        assert _numbers(inbox.query_one("#my-mrs-table", MRTable)) == expected
        await pilot.press("3")
        await settle(app)
        all_open = inbox.query_one("#all-open-table", MRTable)
        assert _numbers(all_open) == expected
        assert inbox._all_open_status_text.startswith(
            f"{len(expected)} open MR{'' if len(expected) == 1 else 's'}"
        )


# -- [editor] command and external_editor_enabled ---------------------------


def test_find_editor_prefers_the_configured_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(external_editor.shutil, "which", lambda name: name)
    env = {"VISUAL": "visual-editor", "EDITOR": "env-editor"}
    config = Config(editor_command="code --wait")
    assert find_editor(config, environ=env) == "code --wait"
    assert find_editor(Config(), environ=env) == "visual-editor"
    assert find_editor(Config(), environ={"EDITOR": "env-editor"}) == "env-editor"
    assert find_editor(Config(editor_command="  "), environ={}) == "nvim"


def test_find_editor_falls_back_to_the_first_editor_on_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        external_editor.shutil, "which", lambda name: name if name == "vi" else None
    )
    assert find_editor(Config(), environ={}) == "vi"
    assert find_editor(Config(), environ={}, fallbacks=("less",)) is None


def test_editor_argv_reports_why_no_editor_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(external_editor.shutil, "which", lambda name: None)
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.delenv("EDITOR", raising=False)
    cases = [
        (
            Config(external_editor_enabled=False, editor_command="vim"),
            EDITOR_DISABLED_MESSAGE,
        ),
        (Config(), NO_EDITOR_MESSAGE),
        (Config(editor_command="vim 'unclosed"), BAD_COMMAND_MESSAGE),
    ]
    for config, message in cases:
        with pytest.raises(EditorUnavailableError) as caught:
            editor_argv(config)
        assert caught.value.message == message
    assert editor_argv(Config(editor_command="code --wait")) == ["code", "--wait"]

    monkeypatch.setattr(external_editor.platform, "system", lambda: "Windows")
    with pytest.raises(EditorUnavailableError) as caught:
        editor_argv(Config(editor_command="code --wait"))
    assert caught.value.message == WINDOWS_MESSAGE


def _record_runs(
    monkeypatch: pytest.MonkeyPatch, edit: Callable[[Path], None] | None = None
) -> list[list[str]]:
    """Capture each editor start; ``edit`` may change the file in between."""
    runs: list[list[str]] = []

    def run(command: list[str], *, check: bool) -> None:
        assert check is False
        runs.append(list(command))
        if edit is not None:
            edit(Path(command[-1]))

    monkeypatch.setattr(external_editor.subprocess, "run", run)
    monkeypatch.setenv("EDITOR", "env-editor")
    monkeypatch.delenv("VISUAL", raising=False)
    return runs


class _CommentApp(App[None]):
    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config

    def compose(self) -> ComposeResult:
        yield CommentEditor(id="comment-editor")


@pytest.mark.asyncio
async def test_f2_in_the_comment_editor_starts_the_configured_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs = _record_runs(monkeypatch, lambda path: path.write_text("edited body"))
    app = _CommentApp(Config(editor_command="my-editor --wait"))
    monkeypatch.setattr(app, "suspend", lambda: nullcontext())
    async with app.run_test() as pilot:
        editor = app.query_one(CommentEditor)
        editor.open_general()
        await pilot.pause()
        editor.action_external_editor()
        await pilot.pause()
        assert app.query_one("#comment-input", TextArea).text == "edited body"
    assert len(runs) == 1
    assert runs[0][:2] == ["my-editor", "--wait"]
    assert not Path(runs[0][2]).exists()


@pytest.mark.asyncio
async def test_f2_in_the_comment_editor_is_off_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs = _record_runs(monkeypatch)
    app = _CommentApp(Config(external_editor_enabled=False))
    async with app.run_test(notifications=True) as pilot:
        editor = app.query_one(CommentEditor)
        editor.open_general()
        await pilot.pause()
        await pilot.press("f2")
        await pilot.pause()
        assert EDITOR_DISABLED_MESSAGE in _messages(app)
    assert runs == []


class _PipelineApp(App[None]):
    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config

    def compose(self) -> ComposeResult:
        yield PipelinePanel(id="pipeline-panel")


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_f2_in_the_job_log_follows_the_editor_settings(
    monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    runs = _record_runs(monkeypatch)
    app = _PipelineApp(
        Config(editor_command="log-viewer", external_editor_enabled=enabled)
    )
    monkeypatch.setattr(app, "suspend", lambda: nullcontext())
    async with app.run_test(notifications=True) as pilot:
        panel = app.query_one(PipelinePanel)
        panel._view_level = 2
        panel._job_log_text = "log line"
        panel.action_open_in_editor()
        await pilot.pause()
        messages = _messages(app)
    if enabled:
        assert [run[0] for run in runs] == ["log-viewer"]
        assert EDITOR_DISABLED_MESSAGE not in messages
    else:
        assert runs == []
        assert EDITOR_DISABLED_MESSAGE in messages


async def _press_f3_on_an_addition(app: TongsApp, pilot: Pilot) -> None:
    option_list = app.screen.query_one(DiffOptionList)
    option_list.focus()
    row = next(
        index
        for index, line in sorted(option_list._line_map.items())
        if line.line_type is LineType.ADDITION
    )
    option_list.highlighted = row
    await pilot.pause()
    await pilot.press("f3")
    await pilot.pause()


@pytest.mark.asyncio
async def test_f3_suggestion_starts_the_configured_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = _record_runs(monkeypatch)
    app, _forge, http = await _build(tmp_path, GITHUB)
    app.config.editor_command = "suggest-editor -w"
    monkeypatch.setattr(app, "suspend", lambda: nullcontext())
    async with http, app.run_test(size=(200, 60), notifications=True) as pilot:
        await _open_diff(app, pilot)
        await _press_f3_on_an_addition(app, pilot)
        await _until(pilot, lambda: bool(runs))
        # The template came back unchanged, so nothing is posted.
        await _until(
            pilot,
            lambda: "No changes made, suggestion cancelled." in _messages(app),
        )
    assert runs[0][:2] == ["suggest-editor", "-w"]
    assert Path(runs[0][2]).name.startswith("tongs-suggest-")
    assert not Path(runs[0][2]).exists()


@pytest.mark.asyncio
async def test_f3_suggestion_is_off_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = _record_runs(monkeypatch)
    app, _forge, http = await _build(tmp_path, GITHUB)
    app.config.external_editor_enabled = False
    async with http, app.run_test(size=(200, 60), notifications=True) as pilot:
        await _open_diff(app, pilot)
        await _press_f3_on_an_addition(app, pilot)
        await _until(pilot, lambda: EDITOR_DISABLED_MESSAGE in _messages(app))
        assert not app.screen.query_one("#comment-editor", CommentEditor).display
    assert runs == []
