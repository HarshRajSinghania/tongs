"""Unchanged lines around a file's hunks: the gap model behind expand-in-place.

A forge diff shows only the hunks of a changed file. The lines between them,
before the first and after the last, are unchanged, so they read the same on
both sides and the new file at the review's head is enough to show them. Old
line numbers follow from the hunk offsets.

This module is pure: no IO, no Textual and no Rich. It takes the new file as a
plain sequence of lines split on ``"\\n"`` only, so a carriage return, a form
feed or a Unicode line separator stays inside its line exactly as the diff
parser keeps it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum

from tongs.diff.models import DiffFile, DiffHunk, DiffLine, FileStatus, LineType

TRAILING_CONTEXT = 3
"""Context lines GitHub and GitLab put after a hunk that does not end the file.

A last hunk with fewer trailing context lines than this reaches the end of the
file, so no gap follows it. With a smaller forge context the trailing gap is
hidden, never mislabelled.
"""

_UNEXPANDABLE_MODES = frozenset({"120000", "160000"})
"""Symlinks and submodules: their "content" is a target or a commit, not text."""


class GapPosition(StrEnum):
    """Where a gap sits relative to the file's hunks."""

    BEFORE = "before"
    BETWEEN = "between"
    AFTER = "after"
    # A long run of context lines inside one hunk that the viewer folds. The
    # forge sent those lines, so showing them needs no file read.
    INSIDE = "inside"


@dataclass(frozen=True, slots=True)
class DiffGap:
    """A run of unchanged lines the diff does not show.

    ``index`` is the gap's position in :func:`file_gaps`, stable whether or
    not the file length is known, because the AFTER gap always comes last.
    ``hunk_index`` is the hunk that follows the gap, or ``len(file.hunks)``
    for the AFTER gap. ``length`` is None only for an AFTER gap read before
    the new file's line count is known.

    An INSIDE gap is a fold within hunk ``hunk_index``. It is not one of
    :func:`file_gaps`, so its ``index`` is :data:`INSIDE_GAP_INDEX`; its
    :attr:`key` tells it apart from other folds.
    """

    index: int
    position: GapPosition
    hunk_index: int
    old_start: int
    new_start: int
    length: int | None

    @property
    def key(self) -> tuple[GapPosition, int]:
        """Identify the gap across redraws, whether or not its length is known."""
        return (self.position, self.new_start)

    def covers(self, new_line: int) -> bool:
        """Whether a new-side line number falls inside this gap."""
        if new_line < self.new_start:
            return False
        return self.length is None or new_line < self.new_start + self.length


INSIDE_GAP_INDEX = -1
"""The ``index`` of every INSIDE gap, which :func:`file_gaps` never returns."""


def inside_gap(hunk_index: int, first: DiffLine, length: int) -> DiffGap:
    """Return the fold that hides ``length`` context lines from ``first`` on."""
    return DiffGap(
        index=INSIDE_GAP_INDEX,
        position=GapPosition.INSIDE,
        hunk_index=hunk_index,
        old_start=first.old_lineno or 0,
        new_start=first.new_lineno or 0,
        length=length,
    )


@dataclass(frozen=True, slots=True)
class HunkBlock:
    """One of the file's hunks, rendered as the forge sent it."""

    hunk_index: int
    hunk: DiffHunk


@dataclass(frozen=True, slots=True)
class GapRow:
    """A collapsed gap, shown as one "unchanged lines" row."""

    gap: DiffGap


@dataclass(frozen=True, slots=True)
class ExpandedBlock:
    """A gap shown in full, as read-only EXPANDED lines."""

    gap: DiffGap
    lines: tuple[DiffLine, ...]


Segment = HunkBlock | GapRow | ExpandedBlock


def _first(start: int, count: int) -> int:
    """First line number a hunk covers on one side."""
    return start if count > 0 else start + 1


def _next(start: int, count: int) -> int:
    """First line number after a hunk on one side."""
    return start + count if count > 0 else start + 1


def _trailing_context(hunk: DiffHunk) -> int:
    """Count the context lines that close a hunk, ignoring no-newline markers."""
    count = 0
    for line in reversed(hunk.lines):
        if line.line_type is LineType.NO_NEWLINE:
            continue
        if line.line_type is not LineType.CONTEXT:
            break
        count += 1
    return count


def _has_after_gap(hunk: DiffHunk) -> bool:
    """Whether unchanged lines can follow the last hunk.

    A no-newline marker on either side means that side ends inside the hunk,
    and lines after the last hunk are unchanged on both sides, so none follow.
    """
    if any(line.line_type is LineType.NO_NEWLINE for line in hunk.lines):
        return False
    return _trailing_context(hunk) >= TRAILING_CONTEXT


def _structural_gaps(file: DiffFile) -> tuple[DiffGap, ...] | None:
    """Gaps before and between hunks plus an open AFTER gap, or None.

    None means the hunks do not describe a consistent file: a gap whose old
    and new lengths differ, or hunks that overlap or run backwards.
    """
    hunks = file.hunks
    if not hunks:
        return None
    gaps: list[DiffGap] = []
    old_next = 1
    new_next = 1
    for hunk_index, hunk in enumerate(hunks):
        if hunk.old_start < 0 or hunk.new_start < 0:
            return None
        if hunk.old_count < 0 or hunk.new_count < 0:
            return None
        old_first = _first(hunk.old_start, hunk.old_count)
        new_first = _first(hunk.new_start, hunk.new_count)
        old_length = old_first - old_next
        new_length = new_first - new_next
        if old_length != new_length or old_length < 0:
            return None
        if old_length > 0:
            gaps.append(
                DiffGap(
                    index=len(gaps),
                    position=GapPosition.BEFORE
                    if hunk_index == 0
                    else GapPosition.BETWEEN,
                    hunk_index=hunk_index,
                    old_start=old_next,
                    new_start=new_next,
                    length=old_length,
                )
            )
        old_next = _next(hunk.old_start, hunk.old_count)
        new_next = _next(hunk.new_start, hunk.new_count)
    if _has_after_gap(hunks[-1]):
        gaps.append(
            DiffGap(
                index=len(gaps),
                position=GapPosition.AFTER,
                hunk_index=len(hunks),
                old_start=old_next,
                new_start=new_next,
                length=None,
            )
        )
    return tuple(gaps)


def is_expandable(file: DiffFile) -> bool:
    """Whether the unchanged lines around this file's hunks can be shown.

    Only a modified or renamed text file with hunks qualifies: an added or
    deleted file already shows every line, and a binary, truncated or
    metadata-only file has no hunks to place lines around. Symlinks and
    submodules hold a target or a commit rather than text. Hunks whose gaps
    disagree between the two sides refuse rather than guess numbers. A file
    whose hunks tongs generated locally qualifies like any other.
    """
    if file.status not in (FileStatus.MODIFIED, FileStatus.RENAMED):
        return False
    if not file.hunks or file.is_binary or file.is_truncated or file.is_metadata_only:
        return False
    if file.old_mode in _UNEXPANDABLE_MODES or file.new_mode in _UNEXPANDABLE_MODES:
        return False
    return _structural_gaps(file) is not None


def file_gaps(file: DiffFile, new_line_count: int | None = None) -> tuple[DiffGap, ...]:
    """Return the file's gaps in file order, or ``()`` when it is not expandable.

    Without ``new_line_count`` the AFTER gap, when the last hunk leaves room
    for one, has an unknown length. With it the AFTER gap gets its length and
    is dropped when the file ends at the last hunk.
    """
    if not is_expandable(file):
        return ()
    gaps = _structural_gaps(file) or ()
    if new_line_count is None or not gaps or gaps[-1].position is not GapPosition.AFTER:
        return gaps
    after = gaps[-1]
    length = new_line_count - after.new_start + 1
    if length <= 0:
        return gaps[:-1]
    return (*gaps[:-1], _with_length(after, length))


def _with_length(gap: DiffGap, length: int) -> DiffGap:
    return DiffGap(
        index=gap.index,
        position=gap.position,
        hunk_index=gap.hunk_index,
        old_start=gap.old_start,
        new_start=gap.new_start,
        length=length,
    )


def verify_new_side(
    file: DiffFile, lines: Sequence[str], ends_with_newline: bool
) -> bool:
    """Check that ``lines`` is the new file the hunks were computed against.

    Every context and added line must sit at its new line number with the same
    content, and a no-newline marker on the new side must agree with how the
    file ends. A mismatch means the file read and the diff disagree, so line
    numbers taken from one would mislabel the other.
    """
    total = len(lines)
    for hunk in file.hunks:
        previous: DiffLine | None = None
        for line in hunk.lines:
            if line.line_type is LineType.NO_NEWLINE:
                # After a context or added line the new side ends there, so
                # the file must lack its final newline and end at that line.
                if (
                    previous is not None
                    and previous.line_type in (LineType.CONTEXT, LineType.ADDITION)
                    and (ends_with_newline or previous.new_lineno != total)
                ):
                    return False
                continue
            previous = line
            if line.line_type not in (LineType.CONTEXT, LineType.ADDITION):
                continue
            number = line.new_lineno
            if number is None or not 1 <= number <= total:
                return False
            if lines[number - 1] != line.content:
                return False
    if not ends_with_newline and total:
        # The last line has no newline. If a hunk shows it, the diff must say
        # so with a marker, which the loop above has already matched.
        for hunk in file.hunks:
            for index, line in enumerate(hunk.lines):
                if (
                    line.line_type in (LineType.CONTEXT, LineType.ADDITION)
                    and line.new_lineno == total
                    and not _followed_by_marker(hunk.lines, index)
                ):
                    return False
    return True


def _followed_by_marker(lines: Sequence[DiffLine], index: int) -> bool:
    following = lines[index + 1 : index + 2]
    return bool(following) and following[0].line_type is LineType.NO_NEWLINE


class FileContextModel:
    """The new file's lines mapped onto one file's gaps.

    Expanded lines are built once, for every gap, when the model is made and
    never rebuilt, so their identity is stable across redraws and threads and
    can key highlight maps by ``id()``.
    """

    def __init__(
        self, file: DiffFile, lines: Sequence[str], ends_with_newline: bool
    ) -> None:
        self.file = file
        self.lines = tuple(lines)
        self.ends_with_newline = ends_with_newline
        self.gaps = file_gaps(file, len(self.lines))
        self._by_index = {gap.index: gap for gap in self.gaps}
        self._expanded: dict[int, tuple[DiffLine, ...]] = {
            gap.index: self._build(gap) for gap in self.gaps
        }

    def _build(self, gap: DiffGap) -> tuple[DiffLine, ...]:
        if not gap.length:
            return ()
        start = gap.new_start - 1
        return tuple(
            DiffLine(
                old_lineno=gap.old_start + offset,
                new_lineno=gap.new_start + offset,
                content=content,
                line_type=LineType.EXPANDED,
            )
            for offset, content in enumerate(self.lines[start : start + gap.length])
        )

    def gap(self, gap_index: int) -> DiffGap | None:
        """Return the gap with this index, or None when the file has none."""
        return self._by_index.get(gap_index)

    def expanded_lines(self, gap_index: int) -> tuple[DiffLine, ...]:
        """Return the gap's lines as read-only EXPANDED diff lines."""
        return self._expanded.get(gap_index, ())

    def all_expanded_lines(self) -> tuple[DiffLine, ...]:
        """Return every gap's expanded lines in file order."""
        return tuple(line for gap in self.gaps for line in self._expanded[gap.index])

    def full_view_rows(self) -> int:
        """Count the code rows the whole-file view shows for this file.

        Every line of the new file shows once, and every deleted line once
        more, so this is the new file's length plus the hunks' deletions.
        """
        deletions = sum(
            1
            for hunk in self.file.hunks
            for line in hunk.lines
            if line.line_type is LineType.DELETION
        )
        return len(self.lines) + deletions


@dataclass(frozen=True, slots=True)
class FileExpansion:
    """Which gaps of one file the reader has opened.

    ``shown`` holds the indices of opened gaps around the hunks and ``folds``
    the ``(new_start, length)`` of opened folds inside them. ``full`` is the
    whole-file view, which shows everything without forgetting the rest, so
    switching it off restores exactly what was open before.
    """

    shown: frozenset[int] = field(default_factory=frozenset)
    full: bool = False
    folds: frozenset[tuple[int, int]] = field(default_factory=frozenset)

    def expand(self, gap_index: int) -> FileExpansion:
        """Return this expansion with one more gap shown."""
        return replace(self, shown=self.shown | {gap_index})

    def reveal_fold(self, gap: DiffGap) -> FileExpansion:
        """Return this expansion with one more in-hunk fold shown."""
        return replace(self, folds=self.folds | {(gap.new_start, gap.length or 0)})

    def collapse(self, gap_index: int) -> FileExpansion:
        """Return this expansion with one gap hidden again.

        Hiding a gap means the reader no longer wants the whole file, so the
        whole-file view turns off and the gaps opened before it stay open.
        """
        return replace(self, shown=self.shown - {gap_index}, full=False)

    def refold(self, new_line: int) -> FileExpansion:
        """Return this expansion with the folds showing a line closed again.

        Like :meth:`collapse`, it turns the whole-file view off, keeping the
        folds and gaps that were open before it.
        """
        folds = frozenset(
            (start, length)
            for start, length in self.folds
            if not start <= new_line < start + length
        )
        return replace(self, folds=folds, full=False)

    def toggle_full(self) -> FileExpansion:
        """Return this expansion with the whole-file view switched."""
        return replace(self, full=not self.full)

    def with_full(self, full: bool) -> FileExpansion:
        """Return this expansion with the whole-file view on or off."""
        return self if self.full is full else replace(self, full=full)

    def is_shown(self, gap_index: int) -> bool:
        """Whether a gap is open, on its own or through the whole-file view."""
        return self.full or gap_index in self.shown

    def reveals(self, line: DiffLine) -> bool:
        """Whether an in-hunk context line must stay visible instead of folding."""
        if self.full:
            return True
        number = line.new_lineno
        if number is None:
            return False
        return any(start <= number < start + length for start, length in self.folds)


def segments(
    file: DiffFile,
    model: FileContextModel | None,
    expansion: FileExpansion,
) -> tuple[Segment, ...]:
    """Interleave hunks, collapsed gaps and expanded gaps in file order.

    Without a model the gaps come from the hunks alone and stay collapsed. A
    file that is not expandable yields only its hunks, exactly as before.
    """
    hunk_blocks = [HunkBlock(index, hunk) for index, hunk in enumerate(file.hunks)]
    if model is not None and model.file is file:
        gaps = model.gaps
    else:
        model = None
        gaps = file_gaps(file)
    if not gaps:
        return tuple(hunk_blocks)
    by_hunk: dict[int, DiffGap] = {gap.hunk_index: gap for gap in gaps}
    result: list[Segment] = []
    for hunk_index in range(len(file.hunks) + 1):
        gap = by_hunk.get(hunk_index)
        if gap is not None:
            if model is not None and expansion.is_shown(gap.index):
                result.append(ExpandedBlock(gap, model.expanded_lines(gap.index)))
            else:
                result.append(GapRow(gap))
        if hunk_index < len(hunk_blocks):
            result.append(hunk_blocks[hunk_index])
    return tuple(result)
