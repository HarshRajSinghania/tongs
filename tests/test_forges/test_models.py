"""Tests for forge data models."""

from __future__ import annotations

import inspect
from dataclasses import fields

from tongs.forges.models import CIStatus, MRDetail, MRSummary, ReviewDecision

# MRSummary's positional fields, then MRDetail's own, in constructor order.
_SUMMARY_POSITIONS = [
    "forge_host",
    "repo_path",
    "local_path",
    "number",
    "title",
    "author",
    "state",
    "is_draft",
    "source_branch",
    "target_branch",
    "ci_status",
    "created_at",
    "updated_at",
    "web_url",
    "comment_count",
    "has_conflicts",
    "labels",
    "review_decision",
    "additions",
    "deletions",
]
_DETAIL_POSITIONS = [
    "description",
    "merge_status",
    "approvals",
    "reviewers",
    "assignees",
    "changes_count",
    "detailed_merge_status",
    "draft_notes_count",
    "status_check_rollup",
    "head_sha",
    "base_sha",
    "start_sha",
    "changes_overflow",
]


def _positional(cls: type) -> list[str]:
    return [
        name
        for name, parameter in inspect.signature(cls).parameters.items()
        if parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    ]


def test_mr_detail_positional_order_is_pinned():
    """A field added to MRSummary must never shift MRDetail's positions."""
    assert _positional(MRSummary) == _SUMMARY_POSITIONS
    assert _positional(MRDetail) == _SUMMARY_POSITIONS + _DETAIL_POSITIONS


def test_the_listed_head_commit_is_keyword_only():
    for cls in (MRSummary, MRDetail):
        parameter = inspect.signature(cls).parameters["head_commit_sha"]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default == ""


def test_mr_detail_revision_fields_are_appended_for_positional_compatibility():
    names = [field.name for field in fields(MRDetail)]
    assert names[-4:-1] == ["head_sha", "base_sha", "start_sha"]


def test_mr_detail_changes_overflow_is_appended_last_and_defaults_false():
    detail_fields = fields(MRDetail)
    assert detail_fields[-1].name == "changes_overflow"
    assert detail_fields[-1].default is False


class TestForgeModels:
    def test_ci_status_values(self):
        assert CIStatus.SUCCESS.value == "success"
        assert CIStatus.FAILED.value == "failed"

    def test_review_decision_values(self):
        assert ReviewDecision.APPROVED.value == "approved"
        assert ReviewDecision.CHANGES_REQUESTED.value == "changes_requested"
