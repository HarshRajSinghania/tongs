"""Tests for tongs.widgets.discussion_list pure functions and logic."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from textual.app import App, ComposeResult

from tongs.diff.models import DiffFile, DiffHunk, DiffLine, FileStatus, LineType
from tongs.forges.models import Discussion, InlineComment, User
from tongs.widgets.diff_panel import DiffRenderer, ResolveRequested
from tongs.widgets.discussion_list import (
    DiscussionPanel,
    DiscussionReplyRequested,
    JumpToDiffDiscussion,
    _render_thread,
    render_diff_snippet,
)
from tongs.widgets.two_press import TwoPressConfirm

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_disc(
    id="d1",
    file_path="test.py",
    new_line=10,
    is_resolved=False,
    is_inline=True,
    body="test",
):
    return Discussion(
        id=id,
        is_inline=is_inline,
        root_comment=InlineComment(
            id=f"c-{id}",
            author=User(username="testuser"),
            body=body,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            file_path=file_path if is_inline else "",
            old_line=None,
            new_line=new_line if is_inline else None,
            is_resolved=is_resolved,
        ),
        is_resolved=is_resolved,
    )


def _ctx(old: int, new: int, content: str = "") -> DiffLine:
    return DiffLine(
        old_lineno=old, new_lineno=new, content=content, line_type=LineType.CONTEXT
    )


def _add(new: int, content: str = "") -> DiffLine:
    return DiffLine(
        old_lineno=None, new_lineno=new, content=content, line_type=LineType.ADDITION
    )


def _del(old: int, content: str = "") -> DiffLine:
    return DiffLine(
        old_lineno=old, new_lineno=None, content=content, line_type=LineType.DELETION
    )


def _make_file(
    hunks: tuple[DiffHunk, ...] = (),
    new_path: str = "file.py",
    language: str = "",
) -> DiffFile:
    return DiffFile(
        old_path=new_path,
        new_path=new_path,
        status=FileStatus.MODIFIED,
        hunks=hunks,
        language=language,
    )


# ===================================================================
# render_diff_snippet
# ===================================================================


class TestRenderDiffSnippet:
    """Tests for render_diff_snippet()."""

    def test_target_line_none_returns_empty(self):
        file = _make_file(
            hunks=(
                DiffHunk(
                    header="@@ -1,3 +1,2 @@",
                    old_start=1,
                    old_count=3,
                    new_start=1,
                    new_count=2,
                    lines=(_ctx(1, 1, "a"), _del(2, "x"), _ctx(3, 2, "c")),
                ),
            )
        )
        assert render_diff_snippet(file, None) == []

    def test_found_target_with_context(self):
        lines = (
            _ctx(1, 1, "line1"),
            _ctx(2, 2, "line2"),
            _ctx(3, 3, "line3"),
            _ctx(4, 4, "line4"),
            _ctx(5, 5, "line5"),
        )
        file = _make_file(
            hunks=(
                DiffHunk(
                    header="@@ -1,5 +1,5 @@",
                    old_start=1,
                    old_count=5,
                    new_start=1,
                    new_count=5,
                    lines=lines,
                ),
            )
        )
        result = render_diff_snippet(file, 3, context=2)
        assert len(result) == 5

    def test_only_the_target_is_marked_and_the_rest_are_padded(self):
        lines = (
            _ctx(1, 1, "line1"),
            _ctx(2, 2, "line2"),
            _ctx(3, 3, "line3"),
        )
        file = _make_file(
            hunks=(
                DiffHunk(
                    header="@@ -1,3 +1,3 @@",
                    old_start=1,
                    old_count=3,
                    new_start=1,
                    new_count=3,
                    lines=lines,
                ),
            )
        )
        rendered = [DiffRenderer("")._render_line(line).plain for line in lines]

        texts = [r.plain for r in render_diff_snippet(file, 2, context=1)]

        assert texts == [
            "  " + rendered[0],
            "> " + rendered[1],
            "  " + rendered[2],
        ]

    def test_matched_by_old_lineno(self):
        lines = (
            _ctx(1, 1, "keep"),
            _del(2, "removed"),
            _del(3, "removed too"),
            _ctx(4, 2, "after"),
        )
        file = _make_file(
            hunks=(
                DiffHunk(
                    header="@@ -1,4 +1,2 @@",
                    old_start=1,
                    old_count=4,
                    new_start=1,
                    new_count=2,
                    lines=lines,
                ),
            )
        )
        # Old line 3 exists only as a deletion: no new line number matches it.
        texts = [r.plain for r in render_diff_snippet(file, 3, context=1)]

        marked = [t for t in texts if t.startswith("> ")]
        assert len(marked) == 1
        assert marked[0].endswith("removed too")

    def test_context_clamped_at_hunk_start(self):
        names = ("first", "second", "third", "fourth", "fifth")
        lines = tuple(_ctx(n, n, name) for n, name in enumerate(names, start=1))
        file = _make_file(
            hunks=(
                DiffHunk(
                    header="@@ -1,5 +1,5 @@",
                    old_start=1,
                    old_count=5,
                    new_start=1,
                    new_count=5,
                    lines=lines,
                ),
            )
        )
        # Target is line 1 (index 0), so context=2 must clamp the start to 0.
        texts = [r.plain for r in render_diff_snippet(file, 1, context=2)]

        assert [t.split()[-1] for t in texts] == ["first", "second", "third"]

    def test_context_clamped_at_hunk_end(self):
        lines = (
            _ctx(1, 1, "first"),
            _ctx(2, 2, "last"),
        )
        file = _make_file(
            hunks=(
                DiffHunk(
                    header="@@ -1,2 +1,2 @@",
                    old_start=1,
                    old_count=2,
                    new_start=1,
                    new_count=2,
                    lines=lines,
                ),
            )
        )
        # Target is line 2 (index 1), context=2 should clamp end to len
        result = render_diff_snippet(file, 2, context=2)
        assert len(result) == 2


# ===================================================================
# _render_thread
# ===================================================================


class TestRenderThread:
    """Tests for _render_thread()."""

    def test_single_comment_no_replies(self):
        disc = _make_disc(body="Hello world")
        lines = [line.plain for line in _render_thread(disc)]
        assert "@testuser" in lines[0]
        assert any("Hello world" in line for line in lines[1:])

    def test_comment_with_replies(self):
        reply = InlineComment(
            id="reply-1",
            author=User(username="reviewer"),
            body="Looks good",
            created_at=datetime(2026, 1, 2, tzinfo=UTC),
            file_path="test.py",
        )
        disc = Discussion(
            id="d-threaded",
            is_inline=True,
            root_comment=InlineComment(
                id="root",
                author=User(username="author"),
                body="Please review",
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
                file_path="test.py",
                new_line=5,
                replies=(reply,),
            ),
        )
        lines = _render_thread(disc)
        plain_texts = [ln.plain for ln in lines]
        # Root comment author should appear
        assert any("@author" in t for t in plain_texts)
        # Reply author should appear, indented
        reply_lines = [t for t in plain_texts if "@reviewer" in t]
        assert len(reply_lines) > 0
        for rl in reply_lines:
            assert rl.startswith("  "), f"Reply not indented: {rl!r}"


# ===================================================================
# DiscussionPanel._apply_filter
# ===================================================================


class TestApplyFilter:
    """Tests for DiscussionPanel._apply_filter()."""

    def _make_panel(self, filter_mode: str = "all") -> DiscussionPanel:
        panel = DiscussionPanel.__new__(DiscussionPanel)
        panel._filter = filter_mode
        return panel

    def test_filter_all_returns_all(self):
        panel = self._make_panel("all")
        discs = [
            _make_disc(id="d1", is_resolved=False),
            _make_disc(id="d2", is_resolved=True),
        ]
        result = panel._apply_filter(discs)
        assert len(result) == 2

    def test_filter_unresolved(self):
        panel = self._make_panel("unresolved")
        discs = [
            _make_disc(id="d1", is_resolved=False),
            _make_disc(id="d2", is_resolved=True),
            _make_disc(id="d3", is_resolved=False),
        ]
        result = panel._apply_filter(discs)
        assert len(result) == 2
        assert all(not d.is_resolved for d in result)

    def test_filter_unresolved_leaves_out_threads_that_cannot_be_resolved(self):
        panel = self._make_panel("unresolved")
        note = replace(_make_disc(id="note", is_inline=False), resolvable=False)
        discs = [_make_disc(id="d1"), note, _make_disc(id="d2", is_resolved=True)]
        result = panel._apply_filter(discs)
        assert [d.id for d in result] == ["d1"]

    def test_filter_resolved(self):
        panel = self._make_panel("resolved")
        discs = [
            _make_disc(id="d1", is_resolved=False),
            _make_disc(id="d2", is_resolved=True),
            _make_disc(id="d3", is_resolved=True),
        ]
        result = panel._apply_filter(discs)
        assert len(result) == 2
        assert all(d.is_resolved for d in result)


# ===================================================================
# DiscussionPanel._sort_discussions
# ===================================================================


class TestSortDiscussions:
    """Tests for DiscussionPanel._sort_discussions()."""

    def _make_panel(self) -> DiscussionPanel:
        panel = DiscussionPanel.__new__(DiscussionPanel)
        return panel

    def test_unresolved_before_resolved(self):
        panel = self._make_panel()
        discs = [
            _make_disc(id="resolved1", is_resolved=True, file_path="a.py", new_line=1),
            _make_disc(
                id="unresolved1", is_resolved=False, file_path="a.py", new_line=1
            ),
        ]
        result = panel._sort_discussions(discs)
        assert not result[0].is_resolved
        assert result[1].is_resolved

    def test_sorted_by_file_path_then_line(self):
        panel = self._make_panel()
        discs = [
            _make_disc(id="d3", file_path="z.py", new_line=1),
            _make_disc(id="d1", file_path="a.py", new_line=5),
            _make_disc(id="d2", file_path="a.py", new_line=1),
        ]
        result = panel._sort_discussions(discs)
        assert result[0].id == "d2"  # a.py:1
        assert result[1].id == "d1"  # a.py:5
        assert result[2].id == "d3"  # z.py:1

    def test_generals_after_inlines(self):
        panel = self._make_panel()
        discs = [
            _make_disc(id="general", is_inline=False),
            _make_disc(id="inline", file_path="a.py", new_line=1),
        ]
        result = panel._sort_discussions(discs)
        assert result[0].id == "inline"
        assert result[1].id == "general"

    def test_mixed_resolved_and_file_sort(self):
        """Resolved status takes priority over file path."""
        panel = self._make_panel()
        discs = [
            _make_disc(id="r-a", is_resolved=True, file_path="a.py", new_line=1),
            _make_disc(id="u-z", is_resolved=False, file_path="z.py", new_line=1),
        ]
        result = panel._sort_discussions(discs)
        # Unresolved z.py should come before resolved a.py
        assert result[0].id == "u-z"
        assert result[1].id == "r-a"

    def test_general_unresolved_before_general_resolved(self):
        panel = self._make_panel()
        discs = [
            _make_disc(id="g-resolved", is_inline=False, is_resolved=True),
            _make_disc(id="g-unresolved", is_inline=False, is_resolved=False),
        ]
        result = panel._sort_discussions(discs)
        assert result[0].id == "g-unresolved"
        assert result[1].id == "g-resolved"


# ===================================================================
# Messages posted by the panel actions
# ===================================================================


class DiscussionPanelApp(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.jumps: list[JumpToDiffDiscussion] = []
        self.replies: list[DiscussionReplyRequested] = []
        self.resolutions: list[ResolveRequested] = []

    def compose(self) -> ComposeResult:
        yield DiscussionPanel(id="discussions")

    def on_jump_to_diff_discussion(self, message: JumpToDiffDiscussion) -> None:
        self.jumps.append(message)

    def on_discussion_reply_requested(self, message: DiscussionReplyRequested) -> None:
        self.replies.append(message)

    def on_resolve_requested(self, message: ResolveRequested) -> None:
        self.resolutions.append(message)


def _deleted_line_disc() -> Discussion:
    """An inline thread on a deleted line: only old_line is set."""
    return Discussion(
        id="d-old",
        is_inline=True,
        root_comment=InlineComment(
            id="c-old",
            author=User(username="alice"),
            body="why remove this?",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            file_path="src/gone.py",
            old_line=7,
            new_line=None,
        ),
    )


@pytest.mark.asyncio
async def test_jump_to_diff_posts_the_old_line_of_a_deleted_line_thread() -> None:
    app = DiscussionPanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(DiscussionPanel)
        panel.set_discussions([_deleted_line_disc()])
        await pilot.pause()
        panel.action_jump_to_diff()
        await pilot.pause()

    assert [(m.discussion_id, m.file_path, m.line) for m in app.jumps] == [
        ("d-old", "src/gone.py", 7)
    ]


@pytest.mark.asyncio
async def test_reply_posts_thread_location_and_root_author() -> None:
    app = DiscussionPanelApp()
    general = _make_disc(id="d-general", is_inline=False)
    async with app.run_test() as pilot:
        panel = app.query_one(DiscussionPanel)
        # Inline threads sort before general ones.
        panel.set_discussions([general, _deleted_line_disc()])
        await pilot.pause()
        panel.action_reply()
        panel.action_next_card()
        panel.action_reply()
        await pilot.pause()

    assert [(m.discussion_id, m.file_path, m.line, m.author) for m in app.replies] == [
        ("d-old", "src/gone.py", 7, "alice"),
        ("d-general", None, None, "testuser"),
    ]


@pytest.mark.asyncio
async def test_next_unresolved_skips_threads_that_cannot_be_resolved() -> None:
    app = DiscussionPanelApp()
    # A GitLab individual note sorts with the open threads but is not one.
    note = replace(
        _make_disc(id="d-note", file_path="a.py", new_line=1), resolvable=False
    )
    discs = [
        note,
        _make_disc(id="d-open", file_path="b.py", new_line=1),
        _make_disc(id="d-done", file_path="a.py", new_line=2, is_resolved=True),
    ]
    async with app.run_test() as pilot:
        panel = app.query_one(DiscussionPanel)
        panel.set_discussions(discs)
        await pilot.pause()
        visited = []
        for _ in range(3):
            panel.action_next_unresolved()
            await pilot.pause()
            focused = panel._get_focused_discussion()
            visited.append(focused.id if focused else None)

    assert visited == ["d-open", "d-open", "d-open"]


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.asyncio
async def test_resolve_pressed_late_rearms_instead_of_resolving() -> None:
    app = DiscussionPanelApp()
    clock = _Clock()
    async with app.run_test() as pilot:
        panel = app.query_one(DiscussionPanel)
        panel._resolve_confirm = TwoPressConfirm(clock=clock)
        panel.set_discussions([_make_disc(id="d-open")])
        await pilot.pause()
        panel.action_resolve()
        clock.now += 6.0
        panel.action_resolve()
        await pilot.pause()
        assert app.resolutions == []
        clock.now += 1.0
        panel.action_resolve()
        await pilot.pause()

    assert [(m.discussion_id, m.resolved) for m in app.resolutions] == [
        ("d-open", True)
    ]


@pytest.mark.asyncio
async def test_resolve_armed_on_one_thread_does_not_resolve_another() -> None:
    app = DiscussionPanelApp()
    discs = [
        _make_disc(id="d-a", file_path="a.py", new_line=1),
        _make_disc(id="d-b", file_path="b.py", new_line=1),
    ]
    async with app.run_test() as pilot:
        panel = app.query_one(DiscussionPanel)
        panel.set_discussions(discs)
        await pilot.pause()
        panel.action_resolve()
        panel.action_next_card()
        panel.action_resolve()
        await pilot.pause()
        assert app.resolutions == []
        panel.action_resolve()
        await pilot.pause()

    assert [m.discussion_id for m in app.resolutions] == ["d-b"]
