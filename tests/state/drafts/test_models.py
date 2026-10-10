"""Tests for immutable draft and anchor models."""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest

from tongs.services import ReviewRevision
from tongs.state.drafts import (
    DiffSide,
    DraftContent,
    GeneralDraftComment,
    InlineAnchor,
    InlineDraftComment,
    context_fingerprint,
)


def make_anchor(revision: ReviewRevision | None = None) -> InlineAnchor:
    return InlineAnchor(
        revision or ReviewRevision("head", "base", "start"),
        "old.py",
        "new.py",
        4,
        5,
        DiffSide.NEW,
        context_fingerprint(("before", "selected", "after")),
        start_line=3,
        start_side=DiffSide.OLD,
    )


def test_context_fingerprint_is_stable_and_preserves_line_boundaries() -> None:
    assert context_fingerprint(("ab", "c")) == context_fingerprint(("ab", "c"))
    assert context_fingerprint(("ab", "c")) != context_fingerprint(("a", "bc"))


@pytest.mark.parametrize(
    "current",
    [
        ReviewRevision("other", "base", "start"),
        ReviewRevision("head", "other", "start"),
        ReviewRevision("head", "base", "other"),
        ReviewRevision("head", "base", None),
    ],
)
def test_anchor_staleness_compares_the_complete_revision(
    current: ReviewRevision,
) -> None:
    anchor = make_anchor()

    assert anchor.assessed_against(current).stale is True
    assert anchor.assessed_against(anchor.revision).stale is False


def test_stale_anchor_is_derived_from_revisions_alone() -> None:
    changed = ReviewRevision("other", "base", "start")
    stale = make_anchor().assessed_against(changed)
    assert stale.stale is True

    # A stale assessment never sticks: an anchor whose captured revision
    # matches the current one is fresh, and one that does not stays stale.
    assert stale.assessed_against(stale.revision).stale is False
    assert stale.assessed_against(changed).stale is True
    persisted = replace(make_anchor(), stale=True)
    assert persisted.assessed_against(persisted.revision).stale is False
    assert persisted.assessed_against(changed).stale is True


def test_content_staleness_is_derived_for_every_inline_anchor() -> None:
    changed = ReviewRevision("other", "base", "start")
    stored = DraftContent(
        comments=(
            GeneralDraftComment(uuid4(), "general"),
            InlineDraftComment(uuid4(), "inline", replace(make_anchor(), stale=True)),
        )
    )

    fresh = stored.assessed_against(make_anchor().revision)
    refused = stored.assessed_against(changed)

    assert [
        comment.anchor.stale
        for comment in fresh.comments
        if isinstance(comment, InlineDraftComment)
    ] == [False]
    assert [
        comment.anchor.stale
        for comment in refused.comments
        if isinstance(comment, InlineDraftComment)
    ] == [True]


def test_content_rejects_duplicate_stable_comment_ids() -> None:
    identifier = uuid4()
    with pytest.raises(ValueError, match="unique"):
        DraftContent(
            comments=(
                GeneralDraftComment(identifier, "one"),
                GeneralDraftComment(identifier, "two"),
            )
        )


def test_anchor_requires_a_real_line_on_selected_side() -> None:
    with pytest.raises(ValueError, match="selected side"):
        InlineAnchor(
            ReviewRevision("head", "base"),
            "old.py",
            "new.py",
            1,
            None,
            DiffSide.NEW,
            "fingerprint",
        )


def test_anchor_rejects_non_integer_line_numbers() -> None:
    with pytest.raises(ValueError, match="positive integers"):
        InlineAnchor(
            ReviewRevision("head", "base"),
            "old.py",
            "new.py",
            1,
            12.0,  # type: ignore[arg-type]
            DiffSide.NEW,
            "fingerprint",
        )
