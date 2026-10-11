"""Generate one file's diff from its two versions when the forge withheld it.

Pure functions: no IO, no Textual, no Rich. The service reads both versions
of the file and runs ``generate_file_diff`` in a worker thread.
"""

from __future__ import annotations

import difflib
from collections.abc import Sequence
from dataclasses import replace
from typing import Protocol

from tongs.diff.models import DiffFile, DiffHunk, FileStatus, LineType
from tongs.diff.parser import parse_diff

#: The most diff lines (hunk headers, lines and markers) one generated file
#: may have.
MAX_OUTPUT_LINES = 100_000
#: The marker git writes after a line that has no final newline.
NO_NEWLINE_MARKER = "\\ No newline at end of file"

# The same fixed header tokens the forge patch converter puts in front of an
# @@-only patch. Paths come from the forge's file, never from these headers.
_SYNTHETIC_HEADERS = "--- a/__tongs_old__\n+++ b/__tongs_new__\n"


class GenerationTooLargeError(Exception):
    """The generated diff has more lines than the caller's limit."""

    def __init__(self, limit_lines: int) -> None:
        super().__init__(f"The generated diff is longer than {limit_lines} lines")
        self.limit_lines = limit_lines


class FileLines(Protocol):
    """One version of a text file, split on ``"\\n"`` only.

    The service's ``FileText`` has this shape; the diff layer takes it
    structurally so it never imports the service layer.
    """

    @property
    def lines(self) -> Sequence[str]: ...

    @property
    def ends_with_newline(self) -> bool: ...


def unified_hunks(
    old_lines: Sequence[str],
    new_lines: Sequence[str],
    *,
    old_ends_with_newline: bool,
    new_ends_with_newline: bool,
    context: int = 3,
    max_output_lines: int = MAX_OUTPUT_LINES,
) -> str:
    """Return the ``@@`` hunks between two versions of a file, as git writes them.

    Lines are compared with their line endings, so a change to the final
    newline is a change to the last line, and ``"\\ No newline at end of
    file"`` follows a line that has none, as in git. The result has no file
    headers and no trailing newline; it is empty when the versions are equal.
    More than ``max_output_lines`` lines raises GenerationTooLargeError.
    """
    if context < 0 or max_output_lines < 0:
        raise ValueError("context and max_output_lines must not be negative")
    old = _with_endings(old_lines, old_ends_with_newline)
    new = _with_endings(new_lines, new_ends_with_newline)
    output: list[str] = []
    diff = difflib.unified_diff(old, new, n=context, lineterm="")
    for index, line in enumerate(diff):
        if index < 2:
            # The "---" and "+++" file headers always come first.
            continue
        if line.startswith("@"):
            output.append(line)
        elif line.endswith("\n"):
            output.append(line[:-1])
        else:
            output.append(line)
            output.append(NO_NEWLINE_MARKER)
        if len(output) > max_output_lines:
            raise GenerationTooLargeError(max_output_lines)
    return "\n".join(output)


def generate_file_diff(
    source: DiffFile,
    old: FileLines | None,
    new: FileLines | None,
    *,
    max_output_lines: int = MAX_OUTPUT_LINES,
) -> DiffFile:
    """Return ``source`` with hunks computed from its two versions.

    ``old`` is None for an added file and ``new`` None for a deleted one.
    Paths, status, modes and language stay those of ``source``; the hunks,
    the line counts and the content flags are recomputed, and the file is
    marked ``generated_locally`` and no longer truncated. Equal versions give
    a file with no hunks, flagged as a mode-only, rename-only or empty change.
    """
    text = unified_hunks(
        old.lines if old is not None else (),
        new.lines if new is not None else (),
        old_ends_with_newline=old.ends_with_newline if old is not None else True,
        new_ends_with_newline=new.ends_with_newline if new is not None else True,
        max_output_lines=max_output_lines,
    )
    hunks: tuple[DiffHunk, ...] = ()
    if text:
        parsed = parse_diff(_SYNTHETIC_HEADERS + text, detect_language=False)
        if parsed:
            hunks = parsed[0].hunks
    additions = sum(
        1
        for hunk in hunks
        for line in hunk.lines
        if line.line_type is LineType.ADDITION
    )
    deletions = sum(
        1
        for hunk in hunks
        for line in hunk.lines
        if line.line_type is LineType.DELETION
    )
    unchanged = not hunks
    is_mode_only = (
        unchanged
        and source.status in (FileStatus.MODIFIED, FileStatus.RENAMED)
        and bool(source.mode_change)
    )
    is_rename_only = (
        unchanged
        and not is_mode_only
        and source.status is FileStatus.RENAMED
        and source.old_path != source.new_path
    )
    return replace(
        source,
        hunks=hunks,
        additions=additions,
        deletions=deletions,
        is_binary=False,
        is_truncated=False,
        is_empty=unchanged and not is_mode_only and not is_rename_only,
        is_mode_only=is_mode_only,
        is_rename_only=is_rename_only,
        is_unavailable=False,
        generated_locally=True,
    )


def _with_endings(lines: Sequence[str], ends_with_newline: bool) -> list[str]:
    """Give every line its ``"\\n"`` back, except a last line that had none."""
    result = [f"{line}\n" for line in lines]
    if result and not ends_with_newline:
        result[-1] = result[-1][:-1]
    return result


__all__ = [
    "MAX_OUTPUT_LINES",
    "NO_NEWLINE_MARKER",
    "FileLines",
    "GenerationTooLargeError",
    "generate_file_diff",
    "unified_hunks",
]
