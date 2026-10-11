"""Bottom-docked comment editor widget."""

from __future__ import annotations

from dataclasses import replace
from typing import ClassVar
from uuid import UUID

from textual.app import ComposeResult
from textual.binding import Binding
from textual.content import Content
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Static, TextArea

from tongs.config import app_config
from tongs.diff.models import DiffFile, DiffLine
from tongs.diff.position import DiffPosition, position_from_diff_line
from tongs.state.drafts import DiffSide
from tongs.widgets.external_editor import (
    EditorUnavailableError,
    edit_text,
    editor_argv,
)


class CommentSubmitted(Message):
    """Fired when an inline comment is submitted."""

    def __init__(self, body: str, position: DiffPosition) -> None:
        super().__init__()
        self.body = body
        self.position = position


class GeneralCommentSubmitted(Message):
    """Fired when a general MR comment is submitted."""

    def __init__(self, body: str) -> None:
        super().__init__()
        self.body = body


class ReplySubmitted(Message):
    """Fired when a reply to an existing discussion is submitted."""

    def __init__(self, discussion_id: str, body: str) -> None:
        super().__init__()
        self.discussion_id = discussion_id
        self.body = body


class DraftCommentEdited(Message):
    """Fired when an existing durable draft comment is edited."""

    def __init__(self, comment_id: UUID, body: str) -> None:
        super().__init__()
        self.comment_id = comment_id
        self.body = body


class CommentEditor(Widget):
    """Bottom-docked comment editor. Shows below content, not as a modal."""

    DEFAULT_CSS = """
    CommentEditor {
        dock: bottom;
        height: auto;
        max-height: 40%;
        border-top: solid $accent;
        background: $surface;
        padding: 0 1;
        display: none;
    }

    CommentEditor TextArea {
        height: auto;
        min-height: 3;
        max-height: 10;
    }

    CommentEditor .editor-header {
        height: 1;
        background: $accent 15%;
        padding: 0 1;
    }

    CommentEditor .editor-hint {
        height: 1;
        color: $text-muted;
    }
    """

    BINDINGS: ClassVar[list] = [
        Binding("ctrl+s", "submit", "Submit", show=True, priority=True),
        Binding("ctrl+j", "submit", "Submit", show=False, priority=True),
        Binding("escape", "cancel", "Cancel", show=True, priority=True),
        Binding("f2", "external_editor", "Editor (F2)", show=True),
    ]

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._mode: str = "general"
        self._file: DiffFile | None = None
        self._line: DiffLine | None = None
        self._position: DiffPosition | None = None
        self._discussion_id: str | None = None
        self._previous_focus: Widget | None = None
        self._defer_close = False
        self._submission_pending = False
        self._draft_comment_id: UUID | None = None

    def compose(self) -> ComposeResult:
        yield Static("", id="editor-header", classes="editor-header")
        yield TextArea("", id="comment-input")
        yield Static(
            "[dim]Ctrl+S / Enter submit | Esc cancel | F2 external editor[/]",
            classes="editor-hint",
        )

    def _save_focus_and_open(self) -> None:
        self._previous_focus = self.app.focused

    def _restore_focus(self) -> None:
        if self._previous_focus is not None:
            self._previous_focus.focus()
            self._previous_focus = None

    def open_general(
        self, *, initial_body: str = "", defer_close: bool = False
    ) -> None:
        """Open for a general MR comment."""
        self._save_focus_and_open()
        self._cancel_pending = False
        self._mode = "general"
        self._file = None
        self._line = None
        self._position = None
        self._configure_submission(defer_close)
        self._draft_comment_id = None
        header = self.query_one("#editor-header", Static)
        header.update("[bold]Add comment[/]")
        text_area = self.query_one("#comment-input", TextArea)
        self._replace_text(initial_body)
        self.display = True
        text_area.focus()

    def open_inline(
        self,
        file: DiffFile,
        line: DiffLine,
        *,
        side: DiffSide | None = None,
        initial_body: str = "",
        defer_close: bool = False,
    ) -> None:
        """Open for an inline comment on a specific diff line."""
        self._save_focus_and_open()
        self._cancel_pending = False
        self._mode = "inline"
        self._file = file
        self._line = line
        position = position_from_diff_line(file, line)
        if side is DiffSide.OLD:
            if line.old_lineno is None:
                raise ValueError("old-side comments require an old line number")
            position = replace(position, side="LEFT")
            display_path = file.old_path
            display_line = line.old_lineno
        elif side is DiffSide.NEW:
            if line.new_lineno is None:
                raise ValueError("new-side comments require a new line number")
            position = replace(position, side="RIGHT")
            display_path = file.new_path
            display_line = line.new_lineno
        else:
            display_path = file.new_path
            display_line = line.new_lineno or line.old_lineno or ""
        self._position = position
        self._configure_submission(defer_close)
        self._draft_comment_id = None
        header = self.query_one("#editor-header", Static)
        header.update(
            Content.from_markup(
                "[bold]Comment on $path:$line[/]", path=display_path, line=display_line
            )
        )
        text_area = self.query_one("#comment-input", TextArea)
        self._replace_text(initial_body)
        self.display = True
        text_area.focus()

    def open_reply(
        self,
        discussion_id: str,
        file: DiffFile,
        line: DiffLine,
        author: str = "",
        *,
        initial_body: str = "",
        defer_close: bool = False,
    ) -> None:
        """Open for replying to an existing discussion thread."""
        self._save_focus_and_open()
        self._cancel_pending = False
        self._mode = "reply"
        self._file = file
        self._line = line
        self._discussion_id = discussion_id
        self._position = None
        self._configure_submission(defer_close)
        self._draft_comment_id = None
        line_num = line.new_lineno or line.old_lineno or ""
        who = f"@{author} on " if author else ""
        header = self.query_one("#editor-header", Static)
        header.update(
            Content.from_markup(
                "[bold]Reply to $who$path:$line[/]",
                who=who,
                path=file.new_path,
                line=line_num,
            )
        )
        text_area = self.query_one("#comment-input", TextArea)
        self._replace_text(initial_body)
        self.display = True
        text_area.focus()

    def open_reply_general(
        self,
        discussion_id: str,
        author: str = "",
        *,
        initial_body: str = "",
        defer_close: bool = False,
    ) -> None:
        """Open for replying to a general (non-inline) discussion."""
        self._save_focus_and_open()
        self._cancel_pending = False
        self._mode = "reply"
        self._file = None
        self._line = None
        self._discussion_id = discussion_id
        self._position = None
        self._configure_submission(defer_close)
        self._draft_comment_id = None
        who = f"@{author}" if author else "thread"
        header = self.query_one("#editor-header", Static)
        header.update(Content.from_markup("[bold]Reply to $who[/]", who=who))
        text_area = self.query_one("#comment-input", TextArea)
        self._replace_text(initial_body)
        self.display = True
        text_area.focus()

    def open_draft_comment(self, comment_id: UUID, body: str, target: str) -> None:
        """Edit an existing draft body while its immutable target stays unchanged."""
        self._save_focus_and_open()
        self._cancel_pending = False
        self._mode = "draft_edit"
        self._file = None
        self._line = None
        self._position = None
        self._discussion_id = None
        self._draft_comment_id = comment_id
        self._configure_submission(True)
        self.query_one("#editor-header", Static).update(
            Content.from_markup("[bold]Edit draft: $target[/]", target=target)
        )
        self._replace_text(body)
        self.display = True
        self.query_one("#comment-input", TextArea).focus()

    def action_submit(self) -> None:
        if self._submission_pending:
            self.app.notify("Draft save is already in progress", severity="warning")
            return
        text_area = self.query_one("#comment-input", TextArea)
        body = text_area.text
        if not body.strip():
            self.app.notify("Comment cannot be empty")
            return
        if not self._defer_close:
            body = body.strip()
        if self._mode == "draft_edit" and self._draft_comment_id is not None:
            self.post_message(DraftCommentEdited(self._draft_comment_id, body))
        elif self._mode == "reply" and self._discussion_id:
            self.post_message(ReplySubmitted(self._discussion_id, body))
        elif self._mode == "inline" and self._position:
            self.post_message(CommentSubmitted(body, self._position))
        else:
            self.post_message(GeneralCommentSubmitted(body))
        if self._defer_close:
            self._submission_pending = True
            text_area.disabled = True
        else:
            self._close()

    def acknowledge_submission(self) -> None:
        """Close only after the controller confirms durable persistence."""
        self._submission_pending = False
        self.query_one("#comment-input", TextArea).disabled = False
        self._close()

    def reject_submission(self, message: str) -> None:
        """Keep caller text visible and editable after failure or cancellation."""
        self._submission_pending = False
        text_area = self.query_one("#comment-input", TextArea)
        text_area.disabled = False
        text_area.focus()
        self.app.notify(message, severity="warning", markup=False)

    _cancel_pending: bool = False

    def action_cancel(self) -> None:
        text_area = self.query_one("#comment-input", TextArea)
        if text_area.text.strip() and not self._cancel_pending:
            self._cancel_pending = True
            self.app.notify("Press Esc again to discard comment", severity="warning")
            return
        self._cancel_pending = False
        self._close()

    def action_external_editor(self) -> None:
        try:
            argv = editor_argv(app_config(self.app))
        except EditorUnavailableError as error:
            self.app.notify(error.message, markup=False)
            return

        text_area = self.query_one("#comment-input", TextArea)
        try:
            new_text = edit_text(
                self.app,
                argv,
                text_area.text,
                suffix=".md",
                prefix="tongs-comment-",
            )
            text_area.clear()
            text_area.insert(new_text)
        except Exception as exc:  # noqa: BLE001 - Report background/action failures without terminating the TUI.
            self.app.notify(f"Editor failed: {exc}", markup=False)

    def _configure_submission(self, defer_close: bool) -> None:
        self._defer_close = defer_close
        self._submission_pending = False
        self.query_one("#comment-input", TextArea).disabled = False

    def _replace_text(self, body: str) -> None:
        text_area = self.query_one("#comment-input", TextArea)
        text_area.clear()
        if body:
            text_area.insert(body)

    def _close(self) -> None:
        self.display = False
        self._submission_pending = False
        self.query_one("#comment-input", TextArea).disabled = False
        self._restore_focus()
