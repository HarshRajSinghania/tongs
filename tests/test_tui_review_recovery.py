"""Textual coverage for returning a paused review submission to editing."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from textual.widgets import Static, TextArea

from tests.test_tui_mr_services import _app, _wait_until
from tests.test_tui_review_mode import _open_detail
from tongs.errors import ValidationError
from tongs.state.drafts import DraftState, DraftVerdict
from tongs.tui_services import TUIDraftTarget
from tongs.views.mr_detail import MRDetailScreen
from tongs.views.review_submit import ReviewSubmitScreen
from tongs.widgets.comment_editor import CommentEditor

SELF_APPROVAL = ValidationError(
    "Validation failed: Can not approve your own pull request"
)


async def _submit_approval(app, pilot, screen: MRDetailScreen) -> None:
    screen.action_review_draft()
    await _wait_until(app, lambda: isinstance(app.screen, ReviewSubmitScreen))
    await pilot.press("v")
    await pilot.press("ctrl+s")
    # ``_draft_busy`` clears last in the submission worker, so waiting on it
    # keeps the next modal press from being dropped.
    await _wait_until(
        app,
        lambda: (
            screen._review_progress is not None
            and screen._review_progress.outcome.value == "paused"
            and not screen._draft_busy
        ),
    )


async def _return_to_editing(app, pilot, screen: MRDetailScreen) -> None:
    screen.action_review_draft()
    await _wait_until(app, lambda: isinstance(app.screen, ReviewSubmitScreen))
    await pilot.pause()
    modal = cast(ReviewSubmitScreen, app.screen)
    help_text = str(modal.query_one("#review-submit-help", Static).render())
    assert "Submission paused" in help_text
    assert "2 to return unconfirmed content to editing" in help_text

    await pilot.press("2")
    assert app.screen is modal
    await pilot.press("2")
    await _wait_until(
        app,
        lambda: (
            screen._review_draft is not None
            and screen._review_draft.state is DraftState.EDITABLE
            and screen._review_progress is None
            and not screen._draft_busy
        ),
    )


async def _reload_drafts(app, screen: MRDetailScreen) -> None:
    revision = screen._current_review_revision
    assert revision is not None
    app.clear_notifications()
    screen._load_review_drafts(revision)
    await app.workers.wait_for_complete()
    messages = [notification.message for notification in app._notifications]
    assert not any("Could not recover review drafts" in item for item in messages)
    target = TUIDraftTarget(screen._review_draft.review, revision)
    assert await app.services.list_draft_recoveries(target) == ()


@pytest.mark.asyncio
async def test_rejected_self_approval_returns_to_editing_with_key_two(
    tmp_path: Path,
) -> None:
    app, forge = _app(tmp_path)
    forge.verdict_error = SELF_APPROVAL

    async with app.run_test(size=(160, 40), notifications=True) as pilot:
        screen = await _open_detail(app, pilot)
        screen.action_review_draft()
        await _wait_until(app, lambda: screen._review_draft is not None)
        draft_id = screen._review_draft.id

        await _submit_approval(app, pilot, screen)
        attempt_id = screen._review_progress.attempt_id
        assert screen._review_draft.state is DraftState.SUBMITTING
        assert len([call for call in forge.calls if call[0] == "verdict"]) == 1

        await _return_to_editing(app, pilot, screen)

        assert screen._review_draft.id == draft_id
        assert screen._review_draft.verdict is DraftVerdict.APPROVE
        assert not app.session.drafts.holds_attempt(attempt_id)
        assert app.services._known_draft_attempts == {}
        await _reload_drafts(app, screen)
        assert screen._review_draft is not None
        assert screen._review_draft.state is DraftState.EDITABLE
        assert screen._review_progress is None
        assert len([call for call in forge.calls if call[0] == "verdict"]) == 1


@pytest.mark.asyncio
async def test_partially_confirmed_paused_review_keeps_only_unconfirmed_content(
    tmp_path: Path,
) -> None:
    app, forge = _app(tmp_path)
    forge.verdict_error = SELF_APPROVAL

    async with app.run_test(size=(160, 40), notifications=True) as pilot:
        screen = await _open_detail(app, pilot)
        screen.action_review_draft()
        await _wait_until(app, lambda: screen._review_draft is not None)
        screen.action_add_comment()
        editor = screen.query_one("#comment-editor", CommentEditor)
        editor.query_one("#comment-input", TextArea).text = "confirmed comment"
        editor.action_submit()
        await _wait_until(
            app,
            lambda: (
                screen._review_draft is not None
                and len(screen._review_draft.comments) == 1
            ),
        )

        await _submit_approval(app, pilot, screen)
        assert screen._review_progress.completed_step_ids != ()
        assert screen._review_draft.state is DraftState.PARTIAL

        await _return_to_editing(app, pilot, screen)

        assert screen._review_draft.comments == ()
        assert screen._review_draft.verdict is DraftVerdict.APPROVE
        await _reload_drafts(app, screen)
        assert screen._review_draft.state is DraftState.EDITABLE
        assert len([call for call in forge.calls if call[0] == "comment"]) == 1
        assert len([call for call in forge.calls if call[0] == "verdict"]) == 1
