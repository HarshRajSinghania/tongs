"""Wire projections for immutable paged desktop diff snapshots."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from enum import StrEnum

from tongs.desktop.protocol.messages import JsonObject
from tongs.diff.alignment import align_hunk
from tongs.diff.models import DiffFile, DiffHunk, DiffLine, SplitDiffRow


class DiffLayout(StrEnum):
    """Supported renderer projections for a diff snapshot."""

    UNIFIED = "unified"
    SPLIT = "split"


def flatten_diff(
    files: tuple[DiffFile, ...], layout: DiffLayout
) -> tuple[JsonObject, ...]:
    """Serialize files using one immutable projection for the whole snapshot."""
    entries: list[JsonObject] = []
    for file_index, file in enumerate(files):
        entries.append(_file_wire(file_index, file))
        for hunk_index, hunk in enumerate(file.hunks):
            entries.append(_hunk_wire(file_index, hunk_index, hunk))
            if layout is DiffLayout.UNIFIED:
                entries.extend(
                    _line_wire(file_index, hunk_index, line) for line in hunk.lines
                )
            else:
                entries.extend(
                    _split_row_wire(file_index, hunk_index, row_index, row)
                    for row_index, row in enumerate(align_hunk(hunk))
                )
    return tuple(entries)


def _file_wire(file_index: int, file: DiffFile) -> JsonObject:
    # The wire form of an undetected language is null, not the empty string the
    # core model uses.  Every desktop row field declared as nullable text must
    # be either null or non-empty, and one file that breaks that contract makes
    # the client reject the whole diff page.
    return {
        "kind": "file",
        "file_index": file_index,
        "old_path": file.old_path,
        "new_path": file.new_path,
        "status": file.status.value,
        "additions": file.additions,
        "deletions": file.deletions,
        "is_binary": file.is_binary,
        "language": file.language or None,
        "is_truncated": file.is_truncated,
        "is_empty": file.is_empty,
        "is_mode_only": file.is_mode_only,
        "is_rename_only": file.is_rename_only,
        "is_unavailable": file.is_unavailable,
    }


def _hunk_wire(file_index: int, hunk_index: int, hunk: DiffHunk) -> JsonObject:
    return {
        "kind": "hunk",
        "file_index": file_index,
        "hunk_index": hunk_index,
        "header": hunk.header,
        "old_start": hunk.old_start,
        "old_count": hunk.old_count,
        "new_start": hunk.new_start,
        "new_count": hunk.new_count,
        "context_text": hunk.context_text,
    }


def _line_wire(file_index: int, hunk_index: int, line: DiffLine) -> JsonObject:
    return {
        "kind": "line",
        "file_index": file_index,
        "hunk_index": hunk_index,
        "old_line": line.old_lineno,
        "new_line": line.new_lineno,
        "content": line.content,
        "line_type": line.line_type.value,
    }


def _split_row_wire(
    file_index: int,
    hunk_index: int,
    row_index: int,
    row: SplitDiffRow,
) -> JsonObject:
    return {
        "kind": "split",
        "file_index": file_index,
        "hunk_index": hunk_index,
        "row_index": row_index,
        "old": _cell_wire(row.old, "old" if row.old_anchor is not None else None),
        "new": _cell_wire(row.new, "new" if row.new_anchor is not None else None),
    }


def _cell_wire(line: DiffLine | None, anchor_side: str | None) -> JsonObject | None:
    if line is None:
        return None
    return {
        "old_line": line.old_lineno,
        "new_line": line.new_lineno,
        "content": line.content,
        "line_type": line.line_type.value,
        "anchor_side": anchor_side,
    }


_DEFAULT_CACHED_REVIEWS = 4
_DEFAULT_CACHED_LINES = 100_000
_DEFAULT_SNAPSHOTS_PER_REVIEW = 8


@dataclass(frozen=True, slots=True)
class CachedDiff:
    """The converted files of one diff read and the revision they belong to."""

    revision: JsonObject
    files: tuple[DiffFile, ...]


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    diff: CachedDiff
    line_count: int
    snapshot_ids: tuple[str, ...]


class ConvertedDiffCache:
    """Keep recent converted diffs so a layout switch needs no forge read.

    Each entry belongs to one review handle and holds the files of one diff
    read with the revision (head, base and start SHAs) it was read at. Only a
    request that names a snapshot projected from that very read can reuse it,
    so a reused projection always shows the revision the reader already sees.
    A fresh read of the review (an open without a snapshot, which is what the
    renderer's Refresh sends) drops the entry first, and so does a change event
    for the review. The cache holds at most ``max_reviews`` reviews and
    ``max_lines`` diff lines in all, dropping the least recently used review
    first; a diff larger than ``max_lines`` alone is not kept.
    """

    def __init__(
        self,
        *,
        max_reviews: int = _DEFAULT_CACHED_REVIEWS,
        max_lines: int = _DEFAULT_CACHED_LINES,
        max_snapshots_per_review: int = _DEFAULT_SNAPSHOTS_PER_REVIEW,
    ) -> None:
        if max_reviews <= 0 or max_lines <= 0 or max_snapshots_per_review <= 0:
            raise ValueError("diff cache limits must be positive")
        self._max_reviews = max_reviews
        self._max_lines = max_lines
        self._max_snapshots = max_snapshots_per_review
        self._entries: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._lines = 0

    def store(
        self,
        review_handle: str,
        snapshot_id: str,
        revision: JsonObject,
        files: tuple[DiffFile, ...],
    ) -> None:
        """Keep the files of a fresh read, replacing the review's older entry."""
        self.drop(review_handle)
        line_count = _line_count(files)
        if line_count > self._max_lines:
            return
        while self._entries and (
            len(self._entries) >= self._max_reviews
            or self._lines + line_count > self._max_lines
        ):
            _handle, evicted = self._entries.popitem(last=False)
            self._lines -= evicted.line_count
        self._entries[review_handle] = _CacheEntry(
            CachedDiff(revision, files), line_count, (snapshot_id,)
        )
        self._lines += line_count

    def lookup(self, review_handle: str, snapshot_id: str) -> CachedDiff | None:
        """Return the cached read ``snapshot_id`` was projected from, if kept."""
        entry = self._entries.get(review_handle)
        if entry is None or snapshot_id not in entry.snapshot_ids:
            return None
        self._entries.move_to_end(review_handle)
        return entry.diff

    def remember(self, review_handle: str, snapshot_id: str) -> None:
        """Record another snapshot projected from the review's cached read."""
        entry = self._entries.get(review_handle)
        if entry is None:
            return
        snapshot_ids = (*entry.snapshot_ids, snapshot_id)[-self._max_snapshots :]
        self._entries[review_handle] = _CacheEntry(
            entry.diff, entry.line_count, snapshot_ids
        )

    def drop(self, review_handle: str) -> None:
        entry = self._entries.pop(review_handle, None)
        if entry is not None:
            self._lines -= entry.line_count

    def clear(self) -> None:
        self._entries.clear()
        self._lines = 0


def _line_count(files: tuple[DiffFile, ...]) -> int:
    return sum(len(hunk.lines) + 1 for file in files for hunk in file.hunks) + len(
        files
    )


__all__ = ["CachedDiff", "ConvertedDiffCache", "DiffLayout", "flatten_diff"]
