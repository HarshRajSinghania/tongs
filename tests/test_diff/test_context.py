"""Gap model tests for expanding unchanged lines around a file's hunks.

The fixtures are real ``git diff -U3`` (and ``-U0`` where named) hunks over
the old and new file texts they were generated from, so the numbers checked
here are the numbers git itself wrote.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from tongs.diff.context import (
    DiffGap,
    ExpandedBlock,
    FileContextModel,
    FileExpansion,
    GapPosition,
    GapRow,
    HunkBlock,
    file_gaps,
    is_expandable,
    segments,
    verify_new_side,
)
from tongs.diff.models import DiffFile, DiffHunk, DiffLine, FileStatus, LineType
from tongs.diff.parser import parse_diff

# Generated once with ``git diff --no-index -U3`` (``-U0`` for the ``_u0``
# cases); each entry is (old text, new text, the hunks git wrote).
FIXTURES: dict[str, tuple[str, str, str]] = {
    "two_hunks": (
        "line 1\nline 2\nline 3\nline 4\nline 5\nline 6\nline 7\nline 8\nline 9\nline 10\nline 11\nline 12\nline 13\nline 14\nline 15\nline 16\nline 17\nline 18\nline 19\nline 20\nline 21\nline 22\nline 23\nline 24\nline 25\nline 26\nline 27\nline 28\nline 29\nline 30\n",
        "line 1\nline 2\nline 3\nline 4\nline 5\nline 6\nline 7\nline 8\nline 9\nchanged 10\nline 11\nline 12\nline 13\nline 14\nline 15\nline 16\nline 17\nline 18\nline 19\nchanged 20\nline 21\nline 22\nline 23\nline 24\nline 25\nline 26\nline 27\nline 28\nline 29\nline 30\n",
        "@@ -7,7 +7,7 @@ line 6\n line 7\n line 8\n line 9\n-line 10\n+changed 10\n line 11\n line 12\n line 13\n@@ -17,7 +17,7 @@ line 16\n line 17\n line 18\n line 19\n-line 20\n+changed 20\n line 21\n line 22\n line 23\n",
    ),
    "first_line": (
        "line 1\nline 2\nline 3\nline 4\nline 5\nline 6\nline 7\nline 8\nline 9\nline 10\nline 11\nline 12\nline 13\nline 14\nline 15\nline 16\nline 17\nline 18\nline 19\nline 20\nline 21\nline 22\nline 23\nline 24\nline 25\nline 26\nline 27\nline 28\nline 29\nline 30\n",
        "changed 1\nline 2\nline 3\nline 4\nline 5\nline 6\nline 7\nline 8\nline 9\nline 10\nline 11\nline 12\nline 13\nline 14\nline 15\nline 16\nline 17\nline 18\nline 19\nline 20\nline 21\nline 22\nline 23\nline 24\nline 25\nline 26\nline 27\nline 28\nline 29\nline 30\n",
        "@@ -1,4 +1,4 @@\n-line 1\n+changed 1\n line 2\n line 3\n line 4\n",
    ),
    "end_of_file": (
        "line 1\nline 2\nline 3\nline 4\nline 5\nline 6\nline 7\nline 8\nline 9\nline 10\nline 11\nline 12\nline 13\nline 14\nline 15\nline 16\nline 17\nline 18\nline 19\nline 20\nline 21\nline 22\nline 23\nline 24\nline 25\nline 26\nline 27\nline 28\nline 29\nline 30\n",
        "line 1\nline 2\nline 3\nline 4\nline 5\nline 6\nline 7\nline 8\nline 9\nline 10\nline 11\nline 12\nline 13\nline 14\nline 15\nline 16\nline 17\nline 18\nline 19\nline 20\nline 21\nline 22\nline 23\nline 24\nline 25\nline 26\nline 27\nline 28\nline 29\nchanged 30\nadded 31\n",
        "@@ -27,4 +27,5 @@ line 26\n line 27\n line 28\n line 29\n-line 30\n+changed 30\n+added 31\n",
    ),
    "middle_no_final_newline_both": (
        "line 1\nline 2\nline 3\nline 4\nline 5\nline 6\nline 7\nline 8\nline 9\nline 10\nline 11\nline 12\nline 13\nline 14\nline 15\nline 16\nline 17\nline 18\nline 19\nline 20\nline 21\nline 22\nline 23\nline 24\nline 25\nline 26\nline 27\nline 28\nline 29\nline 30",
        "line 1\nline 2\nline 3\nline 4\nline 5\nline 6\nline 7\nline 8\nline 9\nline 10\nline 11\nline 12\nline 13\nline 14\nchanged 15\nline 16\nline 17\nline 18\nline 19\nline 20\nline 21\nline 22\nline 23\nline 24\nline 25\nline 26\nline 27\nline 28\nline 29\nline 30",
        "@@ -12,7 +12,7 @@ line 11\n line 12\n line 13\n line 14\n-line 15\n+changed 15\n line 16\n line 17\n line 18\n",
    ),
    "new_loses_final_newline": (
        "line 1\nline 2\nline 3\nline 4\nline 5\nline 6\nline 7\nline 8\nline 9\nline 10\nline 11\nline 12\nline 13\nline 14\nline 15\nline 16\nline 17\nline 18\nline 19\nline 20\nline 21\nline 22\nline 23\nline 24\nline 25\nline 26\nline 27\nline 28\nline 29\nline 30\n",
        "line 1\nline 2\nline 3\nline 4\nline 5\nline 6\nline 7\nline 8\nline 9\nline 10\nline 11\nline 12\nline 13\nline 14\nline 15\nline 16\nline 17\nline 18\nline 19\nline 20\nline 21\nline 22\nline 23\nline 24\nline 25\nline 26\nline 27\nline 28\nchanged 29\nline 30",
        "@@ -26,5 +26,5 @@ line 25\n line 26\n line 27\n line 28\n-line 29\n-line 30\n+changed 29\n+line 30\n\\ No newline at end of file\n",
    ),
    "three_after": (
        "line 1\nline 2\nline 3\nline 4\nline 5\nline 6\nline 7\nline 8\nline 9\nline 10\nline 11\nline 12\nline 13\nline 14\nline 15\nline 16\nline 17\nline 18\nline 19\nline 20\nline 21\nline 22\nline 23\nline 24\nline 25\nline 26\nline 27\nline 28\nline 29\nline 30\n",
        "line 1\nline 2\nline 3\nline 4\nline 5\nline 6\nline 7\nline 8\nline 9\nline 10\nline 11\nline 12\nline 13\nline 14\nline 15\nline 16\nline 17\nline 18\nline 19\nline 20\nline 21\nline 22\nline 23\nline 24\nline 25\nline 26\nchanged 27\nline 28\nline 29\nline 30\n",
        "@@ -24,7 +24,7 @@ line 23\n line 24\n line 25\n line 26\n-line 27\n+changed 27\n line 28\n line 29\n line 30\n",
    ),
    "zero_count_u0": (
        "line 1\nline 2\nline 3\nline 4\nline 5\nline 6\nline 7\nline 8\nline 9\nline 10\nline 11\nline 12\nline 13\nline 14\nline 15\nline 16\nline 17\nline 18\nline 19\nline 20\nline 21\nline 22\nline 23\nline 24\nline 25\nline 26\nline 27\nline 28\nline 29\nline 30\n",
        "inserted a\ninserted b\nline 1\nline 2\nline 3\nline 4\nline 5\nline 6\nline 7\nline 8\nline 9\nline 10\nline 11\nline 14\nline 15\nline 16\nline 17\nline 18\nline 19\nline 20\ninserted c\nline 21\nline 22\nline 23\nline 24\nline 25\nline 26\nline 27\nline 28\nline 29\nline 30\n",
        "@@ -0,0 +1,2 @@\n+inserted a\n+inserted b\n@@ -12,2 +13,0 @@ line 11\n-line 12\n-line 13\n@@ -20,0 +21 @@ line 20\n+inserted c\n",
    ),
    "delete_start_u0": (
        "line 1\nline 2\nline 3\nline 4\nline 5\nline 6\nline 7\nline 8\nline 9\nline 10\nline 11\nline 12\nline 13\nline 14\nline 15\nline 16\nline 17\nline 18\nline 19\nline 20\nline 21\nline 22\nline 23\nline 24\nline 25\nline 26\nline 27\nline 28\nline 29\nline 30\n",
        "line 3\nline 4\nline 5\nline 6\nline 7\nline 8\nline 9\nline 10\nline 11\nline 12\nline 13\nline 14\nline 15\nline 16\nline 17\nline 18\nline 19\nline 20\nline 21\nline 22\nline 23\nline 24\nline 25\nline 26\nline 27\nline 28\nline 29\nline 30\n",
        "@@ -1,2 +0,0 @@\n-line 1\n-line 2\n",
    ),
    "crlf": (
        "line 1\r\nline 2\r\nline 3\r\nline 4\r\nline 5\r\nline 6\r\nline 7\r\nline 8\r\nline 9\r\nline 10\r\nline 11\r\nline 12\r\nline 13\r\nline 14\r\nline 15\r\nline 16\r\nline 17\r\nline 18\r\nline 19\r\nline 20\r\nline 21\r\nline 22\r\nline 23\r\nline 24\r\nline 25\r\nline 26\r\nline 27\r\nline 28\r\nline 29\r\nline 30\r\n",
        "line 1\r\nline 2\r\nline 3\r\nline 4\r\nline 5\r\nline 6\r\nline 7\r\nline 8\r\nline 9\r\nchanged 10\r\nline 11\r\nline 12\r\nline 13\r\nline 14\r\nline 15\r\nline 16\r\nline 17\r\nline 18\r\nline 19\r\nchanged 20\r\nline 21\r\nline 22\r\nline 23\r\nline 24\r\nline 25\r\nline 26\r\nline 27\r\nline 28\r\nline 29\r\nline 30\r\n",
        "@@ -7,7 +7,7 @@ line 6\n line 7\r\n line 8\r\n line 9\r\n-line 10\r\n+changed 10\r\n line 11\r\n line 12\r\n line 13\r\n@@ -17,7 +17,7 @@ line 16\n line 17\r\n line 18\r\n line 19\r\n-line 20\r\n+changed 20\r\n line 21\r\n line 22\r\n line 23\r\n",
    ),
    "odd_separators": (
        "line 1\nline 2\nline 3\nvt\x0bhere\nline 5\nff\x0chere\nline 7\nfs\x1chere\nline 9\nline 10\nline 11\nls\u2028here\nline 13\nline 14\nline 15\nline 16\nline 17\nline 18\nline 19\nline 20\nline 21\nline 22\nps\u2029x\x85y\nline 24\nline 25\nline 26\nline 27\nline 28\nline 29\nline 30\n",
        "line 1\nline 2\nline 3\nvt\x0bhere\nline 5\nff\x0chere\nline 7\nfs\x1chere\nline 9\nchanged 10\nline 11\nls\u2028here\nline 13\nline 14\nline 15\nline 16\nline 17\nchanged 18\nline 19\nline 20\nline 21\nline 22\nps\u2029x\x85y\nline 24\nline 25\nline 26\nline 27\nline 28\nline 29\nline 30\n",
        "@@ -7,7 +7,7 @@ ff\x0chere\n line 7\n fs\x1chere\n line 9\n-line 10\n+changed 10\n line 11\n ls\u2028here\n line 13\n@@ -15,7 +15,7 @@ line 14\n line 15\n line 16\n line 17\n-line 18\n+changed 18\n line 19\n line 20\n line 21\n",
    ),
}


def _split(text: str) -> tuple[list[str], bool]:
    """Split like the file reader: on newlines only, final empty part dropped."""
    parts = text.split("\n")
    ends_with_newline = parts[-1] == ""
    if ends_with_newline:
        parts.pop()
    return parts, ends_with_newline


def _parse(name: str) -> DiffFile:
    hunks = FIXTURES[name][2]
    files = parse_diff(f"--- a/f.txt\n+++ b/f.txt\n{hunks}", detect_language=False)
    assert len(files) == 1
    return files[0]


def _walk(
    segs: tuple[HunkBlock | GapRow | ExpandedBlock, ...],
) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
    """Collect (number, content) per side in display order."""
    old: list[tuple[int, str]] = []
    new: list[tuple[int, str]] = []
    for segment in segs:
        assert not isinstance(segment, GapRow), f"collapsed gap left: {segment}"
        if isinstance(segment, ExpandedBlock):
            for line in segment.lines:
                assert line.line_type is LineType.EXPANDED
                assert line.old_lineno is not None and line.new_lineno is not None
                old.append((line.old_lineno, line.content))
                new.append((line.new_lineno, line.content))
            continue
        for line in segment.hunk.lines:
            if line.line_type in (LineType.CONTEXT, LineType.DELETION):
                assert line.old_lineno is not None
                old.append((line.old_lineno, line.content))
            if line.line_type in (LineType.CONTEXT, LineType.ADDITION):
                assert line.new_lineno is not None
                new.append((line.new_lineno, line.content))
    return old, new


FULL_COVER = [
    "two_hunks",
    "first_line",
    "end_of_file",
    "middle_no_final_newline_both",
    "new_loses_final_newline",
    "three_after",
    "crlf",
    "odd_separators",
]


@pytest.mark.parametrize("name", FULL_COVER)
def test_full_expansion_covers_both_files_exactly_once(name: str) -> None:
    old_text, new_text, _ = FIXTURES[name]
    old_lines, _ = _split(old_text)
    new_lines, new_eol = _split(new_text)
    file = _parse(name)

    assert is_expandable(file)
    assert verify_new_side(file, new_lines, new_eol)
    model = FileContextModel(file, new_lines, new_eol)
    old, new = _walk(segments(file, model, FileExpansion(full=True)))

    assert [number for number, _ in new] == list(range(1, len(new_lines) + 1))
    assert [content for _, content in new] == new_lines
    assert [number for number, _ in old] == list(range(1, len(old_lines) + 1))
    assert [content for _, content in old] == old_lines


@pytest.mark.parametrize("name", ["zero_count_u0", "delete_start_u0"])
def test_zero_context_hunks_cover_both_files_up_to_the_last_hunk(name: str) -> None:
    old_text, new_text, _ = FIXTURES[name]
    old_lines, _ = _split(old_text)
    new_lines, new_eol = _split(new_text)
    file = _parse(name)

    assert verify_new_side(file, new_lines, new_eol)
    model = FileContextModel(file, new_lines, new_eol)
    # Zero context leaves fewer than three trailing lines, so no AFTER gap.
    assert all(gap.position is not GapPosition.AFTER for gap in model.gaps)
    old, new = _walk(segments(file, model, FileExpansion(full=True)))

    assert [number for number, _ in new] == list(range(1, len(new) + 1))
    assert [content for _, content in new] == new_lines[: len(new)]
    assert [number for number, _ in old] == list(range(1, len(old) + 1))
    assert [content for _, content in old] == old_lines[: len(old)]


def test_two_hunks_have_before_between_and_after_gaps() -> None:
    file = _parse("two_hunks")

    assert file_gaps(file) == (
        DiffGap(0, GapPosition.BEFORE, 0, 1, 1, 6),
        DiffGap(1, GapPosition.BETWEEN, 1, 14, 14, 3),
        DiffGap(2, GapPosition.AFTER, 2, 24, 24, None),
    )
    assert file_gaps(file, 30)[-1] == DiffGap(2, GapPosition.AFTER, 2, 24, 24, 7)


def test_first_hunk_at_line_one_has_no_before_gap() -> None:
    file = _parse("first_line")

    assert file_gaps(file) == (DiffGap(0, GapPosition.AFTER, 1, 5, 5, None),)
    assert file_gaps(file, 30) == (DiffGap(0, GapPosition.AFTER, 1, 5, 5, 26),)


def test_last_hunk_at_end_of_file_has_no_after_gap() -> None:
    file = _parse("end_of_file")

    assert file_gaps(file) == (DiffGap(0, GapPosition.BEFORE, 0, 1, 1, 26),)


def test_no_newline_marker_hides_the_after_gap() -> None:
    file = _parse("new_loses_final_newline")

    assert any(
        line.line_type is LineType.NO_NEWLINE
        for hunk in file.hunks
        for line in hunk.lines
    )
    assert [gap.position for gap in file_gaps(file)] == [GapPosition.BEFORE]


def test_after_gap_without_lines_disappears_once_the_length_is_known() -> None:
    file = _parse("three_after")

    assert file_gaps(file)[-1] == DiffGap(1, GapPosition.AFTER, 1, 31, 31, None)
    assert [gap.position for gap in file_gaps(file, 30)] == [GapPosition.BEFORE]


def test_missing_final_newline_on_both_sides_still_counts_the_tail() -> None:
    file = _parse("middle_no_final_newline_both")
    new_lines, new_eol = _split(FIXTURES["middle_no_final_newline_both"][1])

    assert new_eol is False
    model = FileContextModel(file, new_lines, new_eol)
    assert model.gaps[-1] == DiffGap(1, GapPosition.AFTER, 1, 19, 19, 12)
    assert model.expanded_lines(1)[-1].content == "line 30"


def test_zero_count_hunks_from_git_u0() -> None:
    file = _parse("zero_count_u0")

    assert [
        (hunk.old_start, hunk.old_count, hunk.new_start, hunk.new_count)
        for hunk in file.hunks
    ] == [(0, 0, 1, 2), (12, 2, 13, 0), (20, 0, 21, 1)]
    assert file_gaps(file) == (
        DiffGap(0, GapPosition.BETWEEN, 1, 1, 3, 11),
        DiffGap(1, GapPosition.BETWEEN, 2, 14, 14, 7),
    )


def test_zero_count_deletion_at_the_start() -> None:
    file = _parse("delete_start_u0")

    assert file.hunks[0].new_count == 0
    assert file_gaps(file) == ()
    assert is_expandable(file)


def _hunk(
    old_start: int,
    old_count: int,
    new_start: int,
    new_count: int,
    lines: tuple[DiffLine, ...] = (),
) -> DiffHunk:
    header = f"@@ -{old_start},{old_count} +{new_start},{new_count} @@"
    return DiffHunk(header, old_start, old_count, new_start, new_count, lines)


def _file(*hunks: DiffHunk, **changes: object) -> DiffFile:
    file = DiffFile("f.txt", "f.txt", FileStatus.MODIFIED, hunks)
    return replace(file, **changes) if changes else file


def test_pure_insertion_hunk_shape() -> None:
    added = (
        DiffLine(None, 6, "a", LineType.ADDITION),
        DiffLine(None, 7, "b", LineType.ADDITION),
    )
    file = _file(_hunk(5, 0, 6, 2, added))

    assert file_gaps(file) == (DiffGap(0, GapPosition.BEFORE, 0, 1, 1, 5),)


def test_pure_deletion_hunk_shape() -> None:
    removed = (
        DiffLine(5, None, "a", LineType.DELETION),
        DiffLine(6, None, "b", LineType.DELETION),
    )
    file = _file(_hunk(5, 2, 4, 0, removed))

    assert file_gaps(file) == (DiffGap(0, GapPosition.BEFORE, 0, 1, 1, 4),)


def test_unequal_gap_lengths_make_the_file_not_expandable() -> None:
    lines = (DiffLine(5, 8, "x", LineType.CONTEXT),)
    file = _file(_hunk(5, 1, 8, 1, lines))

    assert is_expandable(file) is False
    assert file_gaps(file) == ()
    assert segments(file, None, FileExpansion()) == (HunkBlock(0, file.hunks[0]),)


def test_overlapping_hunks_make_the_file_not_expandable() -> None:
    file = _file(_hunk(5, 4, 5, 4), _hunk(7, 1, 7, 1))

    assert is_expandable(file) is False


@pytest.mark.parametrize(
    "changes",
    [
        {"status": FileStatus.ADDED},
        {"status": FileStatus.DELETED},
        {"is_binary": True},
        {"is_truncated": True},
        {"is_empty": True},
        {"is_unavailable": True},
        {"old_mode": "120000", "new_mode": "120000"},
        {"old_mode": "160000", "new_mode": "160000"},
        {"hunks": ()},
    ],
)
def test_files_that_are_not_expandable(changes: dict[str, object]) -> None:
    file = replace(_parse("two_hunks"), **changes)

    assert is_expandable(file) is False
    assert file_gaps(file) == ()


def test_renamed_and_locally_generated_files_are_expandable() -> None:
    file = _parse("two_hunks")

    assert is_expandable(replace(file, status=FileStatus.RENAMED))
    assert is_expandable(replace(file, generated_locally=True))
    assert is_expandable(replace(file, old_mode="100644", new_mode="100755"))


def test_verify_new_side_refuses_drift() -> None:
    file = _parse("two_hunks")
    new_lines, new_eol = _split(FIXTURES["two_hunks"][1])

    assert verify_new_side(file, new_lines, new_eol)
    drifted = list(new_lines)
    drifted[9] = "something else"
    assert verify_new_side(file, drifted, new_eol) is False
    shifted = ["extra", *new_lines]
    assert verify_new_side(file, shifted, new_eol) is False
    assert verify_new_side(file, new_lines[:18], new_eol) is False


def test_verify_new_side_checks_the_final_newline() -> None:
    file = _parse("new_loses_final_newline")
    new_lines, new_eol = _split(FIXTURES["new_loses_final_newline"][1])

    assert new_eol is False
    assert verify_new_side(file, new_lines, False)
    assert verify_new_side(file, new_lines, True) is False
    assert verify_new_side(file, [*new_lines, "more"], False) is False

    end = _parse("end_of_file")
    end_lines, _ = _split(FIXTURES["end_of_file"][1])
    # The diff shows the last line with no marker, so it ends with a newline.
    assert verify_new_side(end, end_lines, True)
    assert verify_new_side(end, end_lines, False) is False


def test_crlf_and_odd_separators_stay_inside_their_lines() -> None:
    for name, needle in (("crlf", "\r"), ("odd_separators", "\u2029")):
        new_lines, new_eol = _split(FIXTURES[name][1])
        model = FileContextModel(_parse(name), new_lines, new_eol)
        contents = [
            line.content
            for gap in model.gaps
            for line in model.expanded_lines(gap.index)
        ]
        assert any(needle in content for content in contents)
    odd_lines, odd_eol = _split(FIXTURES["odd_separators"][1])
    odd = FileContextModel(_parse("odd_separators"), odd_lines, odd_eol)
    before = odd.expanded_lines(0)
    assert [line.content for line in before] == odd_lines[:6]
    assert before[3].content == "vt\x0bhere"
    assert before[5].content == "ff\x0chere"


def test_expanded_lines_keep_their_identity() -> None:
    file = _parse("two_hunks")
    new_lines, new_eol = _split(FIXTURES["two_hunks"][1])
    model = FileContextModel(file, new_lines, new_eol)

    first = model.expanded_lines(1)
    assert model.expanded_lines(1) is first
    full = FileExpansion(full=True)
    blocks = [
        segment
        for segment in segments(file, model, full)
        if isinstance(segment, ExpandedBlock)
    ]
    again = [
        segment
        for segment in segments(file, model, full)
        if isinstance(segment, ExpandedBlock)
    ]
    assert all(
        a is b
        for left, right in zip(blocks, again, strict=True)
        for a, b in zip(left.lines, right.lines, strict=True)
    )
    assert [(line.old_lineno, line.new_lineno) for line in first] == [
        (14, 14),
        (15, 15),
        (16, 16),
    ]


def test_segments_interleave_rows_in_file_order() -> None:
    file = _parse("two_hunks")
    new_lines, new_eol = _split(FIXTURES["two_hunks"][1])

    collapsed = segments(file, None, FileExpansion())
    assert [type(segment).__name__ for segment in collapsed] == [
        "GapRow",
        "HunkBlock",
        "GapRow",
        "HunkBlock",
        "GapRow",
    ]
    assert collapsed[-1].gap.length is None

    model = FileContextModel(file, new_lines, new_eol)
    one = segments(file, model, FileExpansion().expand(1))
    assert [type(segment).__name__ for segment in one] == [
        "GapRow",
        "HunkBlock",
        "ExpandedBlock",
        "HunkBlock",
        "GapRow",
    ]
    assert one[-1].gap.length == 7


def test_segments_ignore_a_model_built_for_another_file() -> None:
    file = _parse("two_hunks")
    other = _parse("two_hunks")
    new_lines, new_eol = _split(FIXTURES["two_hunks"][1])
    model = FileContextModel(other, new_lines, new_eol)

    segs = segments(file, model, FileExpansion(full=True))
    assert not any(isinstance(segment, ExpandedBlock) for segment in segs)


def test_file_expansion_is_immutable() -> None:
    base = FileExpansion()
    opened = base.expand(2)
    full = opened.toggle_full()

    assert base.shown == frozenset()
    assert opened.shown == frozenset({2})
    assert full.full is True and full.is_shown(7)
    assert full.toggle_full().full is False


def test_collapse_hides_one_gap_and_keeps_the_others() -> None:
    opened = FileExpansion().expand(0).expand(2)

    collapsed = opened.collapse(0)
    assert collapsed.shown == frozenset({2})
    assert not collapsed.is_shown(0) and collapsed.is_shown(2)
    # Hiding a gap that is not open changes nothing; opening it again works.
    assert collapsed.collapse(0) == collapsed
    assert collapsed.expand(0) == opened
    assert opened.shown == frozenset({0, 2})


def test_collapse_in_the_whole_file_view_turns_it_off_and_keeps_the_rest() -> None:
    before = FileExpansion().expand(1).expand(2)
    full = before.with_full(True)

    collapsed = full.collapse(2)
    assert collapsed.full is False
    assert collapsed.shown == frozenset({1})
    assert collapsed.is_shown(1) and not collapsed.is_shown(0)
    # A gap that was not open before the whole-file view stays closed.
    assert full.collapse(0) == before.with_full(False)


def test_refold_closes_only_the_fold_that_shows_the_line() -> None:
    file = _parse("two_hunks")
    first = file.hunks[0].lines[0]
    fold = DiffGap(-1, GapPosition.INSIDE, 0, 10, 10, 5)
    other = DiffGap(-1, GapPosition.INSIDE, 0, 40, 40, 3)
    opened = FileExpansion().reveal_fold(fold).reveal_fold(other).expand(1)

    assert opened.refold(12).folds == frozenset({(40, 3)})
    assert opened.refold(10).folds == frozenset({(40, 3)})
    assert opened.refold(14).folds == frozenset({(40, 3)})
    # Lines outside every opened fold close nothing.
    assert opened.refold(15) == opened
    assert opened.refold(9) == opened
    # Gaps stay as they were, and the whole-file view turns off.
    assert opened.refold(12).shown == frozenset({1})
    assert opened.with_full(True).refold(15) == opened
    assert not opened.refold(12).reveals(replace(first, new_lineno=12))
    assert opened.refold(12).reveals(replace(first, new_lineno=41))
