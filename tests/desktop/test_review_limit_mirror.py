"""The desktop main process mirrors the Python draft limits exactly."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tongs.state.drafts import MAX_DRAFT_BODY_BYTES, MAX_DRAFT_COMMENTS

ROOT = Path(__file__).resolve().parents[2]
REVIEW_TS = ROOT / "desktop" / "src" / "main" / "review.ts"


def _ts_constant(source: str, name: str) -> int:
    matches = re.findall(rf"^const {name} = ([0-9_]+);$", source, re.MULTILINE)
    assert len(matches) == 1, f"{name} must be declared once in {REVIEW_TS.name}"
    return int(matches[0].replace("_", ""))


@pytest.mark.parametrize(
    ("name", "expected"),
    [("MAX_BODY_BYTES", MAX_DRAFT_BODY_BYTES), ("MAX_COMMENTS", MAX_DRAFT_COMMENTS)],
)
def test_review_ts_limit_matches_the_draft_store(name: str, expected: int) -> None:
    source = REVIEW_TS.read_text(encoding="utf-8")

    assert _ts_constant(source, name) == expected
