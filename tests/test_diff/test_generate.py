"""Local diff generation for files whose patch the forge withheld (#382)."""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass

import pytest

from tongs.diff.generate import (
    NO_NEWLINE_MARKER,
    GenerationTooLargeError,
    generate_file_diff,
    unified_hunks,
)
from tongs.diff.models import DiffFile, FileStatus, LineType


@dataclass(frozen=True)
class Text:
    lines: tuple[str, ...]
    ends_with_newline: bool = True


def text(body: str) -> Text:
    """Split ``body`` the way the file read service does."""
    lines = body.split("\n")
    ends = lines[-1] == ""
    if ends:
        lines.pop()
    return Text(tuple(lines), ends)


def truncated(
    status: FileStatus = FileStatus.MODIFIED,
    *,
    old_path: str = "src/app.py",
    new_path: str = "src/app.py",
    old_mode: str | None = None,
    new_mode: str | None = None,
) -> DiffFile:
    return DiffFile(
        old_path=old_path,
        new_path=new_path,
        status=status,
        hunks=(),
        additions=7,
        deletions=3,
        language="python",
        is_truncated=True,
        old_mode=old_mode,
        new_mode=new_mode,
    )


def apply(old: Sequence[str], file: DiffFile) -> list[str]:
    """Apply ``file``'s hunks to ``old`` and check every line number on the way."""
    result: list[str] = []
    position = 1
    for hunk in file.hunks:
        start = hunk.old_start if hunk.old_count else hunk.old_start + 1
        assert start >= position
        result.extend(old[position - 1 : start - 1])
        position = start
        for line in hunk.lines:
            if line.line_type is LineType.CONTEXT:
                assert line.old_lineno == position
                assert old[position - 1] == line.content
                result.append(line.content)
                assert line.new_lineno == len(result)
                position += 1
            elif line.line_type is LineType.DELETION:
                assert line.old_lineno == position
                assert old[position - 1] == line.content
                position += 1
            elif line.line_type is LineType.ADDITION:
                result.append(line.content)
                assert line.new_lineno == len(result)
            else:
                assert line.line_type is LineType.NO_NEWLINE
    result.extend(old[position - 1 :])
    return result


def markers(file: DiffFile) -> list[tuple[LineType, str]]:
    """The line each no-newline marker follows."""
    found: list[tuple[LineType, str]] = []
    for hunk in file.hunks:
        for previous, line in zip(hunk.lines, hunk.lines[1:], strict=False):
            if line.line_type is LineType.NO_NEWLINE:
                found.append((previous.line_type, previous.content))
    return found


def round_trip(old: Text, new: Text, source: DiffFile | None = None) -> DiffFile:
    file = generate_file_diff(source or truncated(), old, new)
    assert apply(old.lines, file) == list(new.lines)
    return file


class TestUnifiedHunks:
    def test_matches_git_for_two_hunks(self) -> None:
        old = [f"line {n}" for n in range(1, 21)]
        new = list(old)
        new[1] = "line 2 changed"
        new[16:17] = ["line 17 a", "line 17 b"]
        assert unified_hunks(
            old, new, old_ends_with_newline=True, new_ends_with_newline=True
        ).split("\n") == (
            [
                "@@ -1,5 +1,5 @@",
                " line 1",
                "-line 2",
                "+line 2 changed",
                " line 3",
                " line 4",
                " line 5",
                "@@ -14,7 +14,8 @@",
                " line 14",
                " line 15",
                " line 16",
                "-line 17",
                "+line 17 a",
                "+line 17 b",
                " line 18",
                " line 19",
                " line 20",
            ]
        )

    def test_equal_versions_give_no_hunks(self) -> None:
        assert (
            unified_hunks(
                ["a", "b"],
                ["a", "b"],
                old_ends_with_newline=True,
                new_ends_with_newline=True,
            )
            == ""
        )

    def test_marker_follows_the_line_without_a_newline(self) -> None:
        assert unified_hunks(
            ["a", "b"],
            ["a", "c"],
            old_ends_with_newline=False,
            new_ends_with_newline=True,
        ).split("\n") == ["@@ -1,2 +1,2 @@", " a", "-b", NO_NEWLINE_MARKER, "+c"]

    def test_unchanged_last_line_without_newline_keeps_its_marker(self) -> None:
        old = ["a", "b", "c", "d", "e"]
        new = ["a", "b", "x", "d", "e"]
        lines = unified_hunks(
            old, new, old_ends_with_newline=False, new_ends_with_newline=False
        ).split("\n")
        assert lines[-2:] == [" e", NO_NEWLINE_MARKER]

    def test_added_file_hunk_starts_at_zero(self) -> None:
        assert unified_hunks(
            [], ["x", "y"], old_ends_with_newline=True, new_ends_with_newline=True
        ).split("\n") == ["@@ -0,0 +1,2 @@", "+x", "+y"]

    def test_output_cap_raises(self) -> None:
        old = [f"old {n}" for n in range(30)]
        new = [f"new {n}" for n in range(30)]
        with pytest.raises(GenerationTooLargeError) as caught:
            unified_hunks(
                old,
                new,
                old_ends_with_newline=True,
                new_ends_with_newline=True,
                max_output_lines=60,
            )
        assert caught.value.limit_lines == 60

    def test_output_at_the_cap_is_kept(self) -> None:
        old = [f"old {n}" for n in range(30)]
        new = [f"new {n}" for n in range(30)]
        hunks = unified_hunks(
            old,
            new,
            old_ends_with_newline=True,
            new_ends_with_newline=True,
            max_output_lines=61,
        )
        assert len(hunks.split("\n")) == 61

    def test_negative_limits_are_refused(self) -> None:
        with pytest.raises(ValueError):
            unified_hunks(
                [],
                [],
                old_ends_with_newline=True,
                new_ends_with_newline=True,
                context=-1,
            )


class TestGenerateFileDiff:
    def test_two_hunk_modified_file(self) -> None:
        old = text("".join(f"line {n}\n" for n in range(1, 31)))
        new_lines = list(old.lines)
        new_lines[2] = "changed 3"
        new_lines[24:26] = ["changed 25"]
        file = round_trip(old, Text(tuple(new_lines)))
        assert len(file.hunks) == 2
        assert (file.additions, file.deletions) == (2, 3)
        assert [
            (h.old_start, h.old_count, h.new_start, h.new_count) for h in file.hunks
        ] == [
            (1, 6, 1, 6),
            (22, 8, 22, 7),
        ]

    def test_metadata_is_carried_and_flags_are_reset(self) -> None:
        source = truncated(
            FileStatus.RENAMED,
            old_path="old/name.py",
            new_path="new/name.py",
            old_mode="100644",
            new_mode="100755",
        )
        file = round_trip(text("a\nb\nc\n"), text("a\nB\nc\n"), source)
        assert (file.old_path, file.new_path) == ("old/name.py", "new/name.py")
        assert file.status is FileStatus.RENAMED
        assert (file.old_mode, file.new_mode) == ("100644", "100755")
        assert file.language == "python"
        assert file.generated_locally is True
        assert file.is_truncated is False
        assert not file.is_metadata_only
        assert (file.additions, file.deletions) == (1, 1)

    def test_added_file(self) -> None:
        file = round_trip(Text(()), text("one\ntwo\n"), truncated(FileStatus.ADDED))
        file_from_none = generate_file_diff(
            truncated(FileStatus.ADDED), None, text("one\ntwo\n")
        )
        assert file_from_none.hunks == file.hunks
        assert (file.hunks[0].old_start, file.hunks[0].old_count) == (0, 0)
        assert (file.additions, file.deletions) == (2, 0)
        assert file.status is FileStatus.ADDED

    def test_deleted_file(self) -> None:
        old = text("one\ntwo\nthree")
        file = generate_file_diff(truncated(FileStatus.DELETED), old, None)
        assert apply(old.lines, file) == []
        assert (file.hunks[0].new_start, file.hunks[0].new_count) == (0, 0)
        assert (file.additions, file.deletions) == (0, 3)
        assert markers(file) == [(LineType.DELETION, "three")]

    def test_final_newline_added(self) -> None:
        file = round_trip(text("a\nb"), text("a\nb\n"))
        assert markers(file) == [(LineType.DELETION, "b")]
        assert (file.additions, file.deletions) == (1, 1)

    def test_final_newline_removed(self) -> None:
        file = round_trip(text("a\nb\n"), text("a\nb"))
        assert markers(file) == [(LineType.ADDITION, "b")]

    def test_crlf_lines_keep_their_carriage_return(self) -> None:
        file = round_trip(text("one\r\ntwo\r\n"), text("one\r\nTWO\r\n"))
        contents = [line.content for line in file.hunks[0].lines]
        assert contents == ["one\r", "two\r", "TWO\r"]

    def test_other_line_separators_stay_inside_lines(self) -> None:
        old = text("a b\nc\x0bd\ne\x0cf\ng\x1ch\n")
        new = text("a b\nc\x0bD\ne\x0cf\ng\x1ch\n")
        file = round_trip(old, new)
        assert [line.content for line in file.hunks[0].lines] == [
            "a b",
            "c\x0bd",
            "c\x0bD",
            "e\x0cf",
            "g\x1ch",
        ]

    def test_diff_syntax_inside_lines_is_content(self) -> None:
        old = text("-- a\n++ b\n@@ -1 +1 @@\n\\ No newline at end of file\n\n")
        new = text("--- a\n+++ b\n@@ -2 +2 @@\n\\ no\n\nx\n")
        round_trip(old, new)

    def test_identical_texts_give_an_empty_file(self) -> None:
        file = generate_file_diff(truncated(), text("a\n"), text("a\n"))
        assert file.hunks == ()
        assert file.is_empty
        assert (file.additions, file.deletions) == (0, 0)

    def test_identical_rename_is_rename_only(self) -> None:
        source = truncated(FileStatus.RENAMED, old_path="a.py", new_path="b.py")
        file = generate_file_diff(source, text("a\n"), text("a\n"))
        assert file.is_rename_only
        assert not file.is_empty

    def test_identical_text_with_a_mode_change_is_mode_only(self) -> None:
        source = truncated(old_mode="100644", new_mode="100755")
        file = generate_file_diff(source, text("a\n"), text("a\n"))
        assert file.is_mode_only
        assert not file.is_empty

    def test_output_cap_raises(self) -> None:
        with pytest.raises(GenerationTooLargeError):
            generate_file_diff(
                truncated(), text("a\nb\n"), text("c\nd\n"), max_output_lines=4
            )

    @pytest.mark.parametrize("seed", range(40))
    def test_random_edits_round_trip(self, seed: int) -> None:
        rng = random.Random(seed)
        vocabulary = ["", "x", "y", "}", "\r", "  return", "-", "+", " ", "\\"]
        old = [rng.choice(vocabulary) for _ in range(rng.randrange(0, 60))]
        new = list(old)
        for _ in range(rng.randrange(0, 8)):
            index = rng.randrange(0, len(new) + 1)
            action = rng.randrange(3)
            if action == 0:
                new.insert(index, rng.choice(vocabulary))
            elif action == 1 and index < len(new):
                del new[index]
            elif index < len(new):
                new[index] = rng.choice(vocabulary) + "!"
        old_text = Text(tuple(old), rng.random() < 0.8 or not old)
        new_text = Text(tuple(new), rng.random() < 0.8 or not new)
        file = round_trip(old_text, new_text)
        expected_markers = 0
        if old and not old_text.ends_with_newline:
            expected_markers += 1
        if new and not new_text.ends_with_newline:
            expected_markers += 1
        if old == new and old_text.ends_with_newline == new_text.ends_with_newline:
            assert file.hunks == ()
        else:
            assert len(markers(file)) <= expected_markers
