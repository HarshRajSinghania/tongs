"""Parse unified diff text into structured DiffFile/DiffHunk/DiffLine objects.

This parser is forge-agnostic. Both GitHub and GitLab produce standard
unified diff format. The parser handles:
- Standard @@ hunk headers with optional function context
- No-newline-at-end-of-file markers
- File mode changes, including sections that change only the mode
- Binary file markers
- Renamed and copied files (with similarity index)
- Multiple files in a single diff
"""

from __future__ import annotations

import os
import re
from functools import lru_cache

from pygments.lexers import get_lexer_for_filename

from tongs.diff.models import DiffFile, DiffHunk, DiffLine, FileStatus, LineType

HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")

FILE_HEADER_RE = re.compile(r"^--- (.+)$")
FILE_HEADER_NEW_RE = re.compile(r"^\+\+\+ (.+)$")

DIFF_GIT_RE = re.compile(r"^diff --git a/(.+) b/(.+)$")
_DIFF_GIT_PREFIX = "diff --git "

# A git file mode such as ``100644``.  Anything else on a mode line is ignored
# rather than shown, so a header can only ever display octal digits.
_MODE_RE = re.compile(r"[0-7]{1,7}")

# Distinct file names whose language lookup is remembered.  Pygments matches a
# lexer on the base name alone, so the cache key loses nothing.
_LANGUAGE_CACHE_SIZE = 4096


def parse_diff(diff_text: str, *, detect_language: bool = True) -> list[DiffFile]:
    """Parse a unified diff string into a list of DiffFile objects.

    ``detect_language`` false leaves every file's language empty, for a caller
    whose header paths are placeholders and that detects the language itself.
    """
    if not diff_text or not diff_text.strip():
        return []

    lines = diff_text.split("\n")
    files: list[DiffFile] = []
    i = 0

    while i < len(lines):
        line = lines[i]

        git_match = DIFF_GIT_RE.match(line)
        if git_match:
            file, consumed = _parse_git_diff_file(lines, i, detect_language)
            if file:
                files.append(file)
            i += consumed
            continue

        old_match = FILE_HEADER_RE.match(line)
        if old_match and i + 1 < len(lines):
            new_match = FILE_HEADER_NEW_RE.match(lines[i + 1])
            if new_match:
                file, consumed = _parse_plain_diff_file(lines, i, detect_language)
                if file:
                    files.append(file)
                i += consumed
                continue

        i += 1

    return files


def _parse_git_diff_file(
    lines: list[str], start: int, detect_language: bool = True
) -> tuple[DiffFile | None, int]:
    """Parse a file section starting from 'diff --git ...'.

    The extended header lines are authoritative where git writes them:
    ``rename from``/``rename to`` and ``copy from``/``copy to`` name the paths
    exactly, so they win over the ambiguous ``diff --git`` line.  A copy is
    kept like a rename, and a section that only changes the file mode is kept
    as a mode-only file instead of being dropped.
    """
    if not DIFF_GIT_RE.match(lines[start]):
        return None, 1

    old_path, new_path = _git_header_paths(lines[start][len(_DIFF_GIT_PREFIX) :])
    i = start + 1
    is_binary = False
    status = FileStatus.MODIFIED
    old_mode: str | None = None
    new_mode: str | None = None
    moved_from: str | None = None
    moved_to: str | None = None

    while i < len(lines):
        line = lines[i]

        if line.startswith(_DIFF_GIT_PREFIX):
            break

        if line.startswith("new file"):
            status = FileStatus.ADDED
            new_mode = _header_mode(line, "new file mode ") or new_mode
            i += 1
            continue

        if line.startswith("deleted file"):
            status = FileStatus.DELETED
            old_mode = _header_mode(line, "deleted file mode ") or old_mode
            i += 1
            continue

        if line.startswith("old mode "):
            old_mode = _header_mode(line, "old mode ")
            i += 1
            continue

        if line.startswith("new mode "):
            new_mode = _header_mode(line, "new mode ")
            i += 1
            continue

        if line.startswith(("rename from ", "copy from ")):
            status = FileStatus.RENAMED
            moved_from = line.split(" from ", 1)[1]
            i += 1
            continue

        if line.startswith(("rename to ", "copy to ")):
            status = FileStatus.RENAMED
            moved_to = line.split(" to ", 1)[1]
            i += 1
            continue

        if line.startswith(("rename from", "rename to", "copy from", "copy to")):
            status = FileStatus.RENAMED
            i += 1
            continue

        if line.startswith(("similarity index", "dissimilarity index")):
            i += 1
            continue

        if line.startswith("index "):
            i += 1
            continue

        if line.startswith("Binary files"):
            is_binary = True
            i += 1
            continue

        if line.startswith("--- "):
            hunks, consumed = _parse_hunks_from(lines, i)
            actual_old = moved_from or _strip_prefix(lines[i][4:])
            actual_new = moved_to or (
                _strip_prefix(lines[i + 1][4:]) if i + 1 < len(lines) else new_path
            )

            additions = sum(
                1 for h in hunks for dl in h.lines if dl.line_type == LineType.ADDITION
            )
            deletions = sum(
                1 for h in hunks for dl in h.lines if dl.line_type == LineType.DELETION
            )

            lang = (
                _detect_language(
                    actual_new if actual_new != "/dev/null" else actual_old
                )
                if detect_language
                else ""
            )

            return (
                DiffFile(
                    old_path=actual_old,
                    new_path=actual_new,
                    status=status,
                    hunks=tuple(hunks),
                    additions=additions,
                    deletions=deletions,
                    is_binary=is_binary,
                    language=lang,
                    is_mode_only=_is_mode_only(
                        status, old_mode, new_mode, bool(hunks) or is_binary
                    ),
                    old_mode=old_mode,
                    new_mode=new_mode,
                ),
                i + consumed - start,
            )

        if line.startswith("+++ "):
            i += 1
            continue

        i += 1

    old_path = moved_from or old_path
    new_path = moved_to or new_path
    is_mode_only = _is_mode_only(status, old_mode, new_mode, is_binary)
    if (
        is_binary
        or is_mode_only
        or status
        in (
            FileStatus.ADDED,
            FileStatus.DELETED,
            FileStatus.RENAMED,
        )
    ):
        lang = _detect_language(new_path) if detect_language else ""
        return (
            DiffFile(
                old_path=old_path,
                new_path=new_path,
                status=status,
                hunks=(),
                is_binary=is_binary,
                language=lang,
                is_mode_only=is_mode_only,
                is_rename_only=(
                    status is FileStatus.RENAMED
                    and not is_binary
                    and not is_mode_only
                    and old_path != new_path
                ),
                old_mode=old_mode,
                new_mode=new_mode,
            ),
            i - start,
        )

    return None, i - start


def _git_header_paths(names: str) -> tuple[str, str]:
    """Split the ``a/<old> b/<new>`` part of a ``diff --git`` line.

    A path may itself contain `` b/``, so the first or last occurrence is not
    reliable.  Without a rename or copy both names are the same path, which
    git writes as two equal halves; that split is unambiguous and is tried
    first.  Otherwise the last separator is used, and the caller replaces both
    paths with the exact ``rename``/``copy`` or ``---``/``+++`` names.
    """
    prefix = "a/"
    separator = " b/"
    if names.startswith(prefix):
        name_length = (len(names) - len(prefix) - len(separator)) // 2
        middle = len(prefix) + name_length
        if (
            name_length > 0
            and len(names) == middle + len(separator) + name_length
            and names[middle : middle + len(separator)] == separator
            and names[len(prefix) : middle] == names[middle + len(separator) :]
        ):
            path = names[len(prefix) : middle]
            return path, path
    match = DIFF_GIT_RE.match(_DIFF_GIT_PREFIX + names)
    if match:
        return match.group(1), match.group(2)
    return names, names


def _header_mode(line: str, prefix: str) -> str | None:
    """Read an octal git file mode from an extended header line."""
    if not line.startswith(prefix):
        return None
    mode = line[len(prefix) :].strip()
    return mode if _MODE_RE.fullmatch(mode) else None


def _is_mode_only(
    status: FileStatus,
    old_mode: str | None,
    new_mode: str | None,
    has_content: bool,
) -> bool:
    """A modified or renamed file whose only change is its mode."""
    return (
        not has_content
        and status in (FileStatus.MODIFIED, FileStatus.RENAMED)
        and old_mode is not None
        and new_mode is not None
        and old_mode != new_mode
    )


def _parse_plain_diff_file(
    lines: list[str], start: int, detect_language: bool = True
) -> tuple[DiffFile | None, int]:
    """Parse a file section starting from '--- ...' (no git prefix)."""
    old_path = _strip_prefix(lines[start][4:])
    new_path = _strip_prefix(lines[start + 1][4:])

    hunks, consumed = _parse_hunks_from(lines, start)

    additions = sum(
        1 for h in hunks for dl in h.lines if dl.line_type == LineType.ADDITION
    )
    deletions = sum(
        1 for h in hunks for dl in h.lines if dl.line_type == LineType.DELETION
    )

    status = FileStatus.MODIFIED
    if old_path == "/dev/null":
        status = FileStatus.ADDED
    elif new_path == "/dev/null":
        status = FileStatus.DELETED

    lang = (
        _detect_language(new_path if new_path != "/dev/null" else old_path)
        if detect_language
        else ""
    )

    return (
        DiffFile(
            old_path=old_path,
            new_path=new_path,
            status=status,
            hunks=tuple(hunks),
            additions=additions,
            deletions=deletions,
            language=lang,
        ),
        consumed,
    )


def _parse_hunks_from(lines: list[str], start: int) -> tuple[list[DiffHunk], int]:
    """Parse all hunks starting from the --- line."""
    i = start
    # Skip --- and +++ lines
    if i < len(lines) and lines[i].startswith("--- "):
        i += 1
    if i < len(lines) and lines[i].startswith("+++ "):
        i += 1

    hunks: list[DiffHunk] = []

    while i < len(lines):
        line = lines[i]

        if line.startswith("diff --git "):
            break

        if (
            line.startswith("--- ")
            and i + 1 < len(lines)
            and lines[i + 1].startswith("+++ ")
        ):
            break

        hunk_match = HUNK_HEADER_RE.match(line)
        if hunk_match:
            hunk, consumed = _parse_single_hunk(lines, i, hunk_match)
            hunks.append(hunk)
            i += consumed
            continue

        i += 1

    return hunks, i - start


def _parse_single_hunk(
    lines: list[str], start: int, hunk_match: re.Match
) -> tuple[DiffHunk, int]:
    """Parse a single hunk starting from the @@ line."""
    old_start = int(hunk_match.group(1))
    old_count = int(hunk_match.group(2) or "1")
    new_start = int(hunk_match.group(3))
    new_count = int(hunk_match.group(4) or "1")
    context_text = hunk_match.group(5).strip()

    diff_lines: list[DiffLine] = []
    old_line = old_start
    new_line = new_start
    old_consumed = 0
    new_consumed = 0
    i = start + 1

    while i < len(lines):
        line = lines[i]

        if line.startswith(("@@ ", "diff --git ")):
            break

        if not line and old_consumed >= old_count and new_consumed >= new_count:
            break

        if _is_file_header_boundary(
            lines, i, old_consumed, new_consumed, old_count, new_count
        ):
            break

        if line.startswith("+"):
            diff_lines.append(
                DiffLine(
                    old_lineno=None,
                    new_lineno=new_line,
                    content=line[1:],
                    line_type=LineType.ADDITION,
                )
            )
            new_line += 1
            new_consumed += 1
        elif line.startswith("-"):
            diff_lines.append(
                DiffLine(
                    old_lineno=old_line,
                    new_lineno=None,
                    content=line[1:],
                    line_type=LineType.DELETION,
                )
            )
            old_line += 1
            old_consumed += 1
        elif line.startswith(" ") or not line:
            diff_lines.append(
                DiffLine(
                    old_lineno=old_line,
                    new_lineno=new_line,
                    content=line[1:] if line else "",
                    line_type=LineType.CONTEXT,
                )
            )
            old_line += 1
            new_line += 1
            old_consumed += 1
            new_consumed += 1
        elif line.startswith("\\"):
            diff_lines.append(
                DiffLine(
                    old_lineno=None,
                    new_lineno=None,
                    content=line,
                    line_type=LineType.NO_NEWLINE,
                )
            )
        else:
            break

        i += 1

    return (
        DiffHunk(
            header=lines[start],
            old_start=old_start,
            old_count=old_count,
            new_start=new_start,
            new_count=new_count,
            lines=tuple(diff_lines),
            context_text=context_text,
        ),
        i - start,
    )


def _is_file_header_boundary(
    lines: list[str],
    index: int,
    old_consumed: int,
    new_consumed: int,
    old_count: int,
    new_count: int,
) -> bool:
    """Recognize a following file header without swallowing source lines.

    A complete hunk can use the normal count gate. For an incomplete hunk,
    only a header pair followed immediately by another hunk header is treated
    as a file boundary. This recovers subsequent files while retaining
    file-header-like source lines that do not have that structure.
    """

    if not (
        index + 1 < len(lines)
        and lines[index].startswith("--- ")
        and lines[index + 1].startswith("+++ ")
    ):
        return False
    if old_consumed >= old_count and new_consumed >= new_count:
        return True
    # These two lines are also the valid unified-diff encoding of a deletion
    # whose source starts with ``--`` followed by an addition starting with
    # ``++``. Preserve them whenever both sides still fit the current hunk.
    # A truncated plain diff can be ambiguous here; declared hunk counts take
    # priority over speculative recovery of a following plain file.
    if old_consumed < old_count and new_consumed < new_count:
        return False
    return index + 2 < len(lines) and HUNK_HEADER_RE.match(lines[index + 2]) is not None


def _strip_prefix(path: str) -> str:
    """Strip a/ or b/ prefix from diff paths."""
    path = path.strip()
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def _detect_language(path: str) -> str:
    """Detect programming language from file path using Pygments."""
    if path == "/dev/null":
        return ""
    return _language_for_name(os.path.basename(path))


@lru_cache(maxsize=_LANGUAGE_CACHE_SIZE)
def _language_for_name(name: str) -> str:
    """Look up one base name's lexer once; Pygments matches on it alone."""
    try:
        lexer = get_lexer_for_filename(name)
        return lexer.aliases[0] if lexer.aliases else lexer.name.lower()
    except Exception:  # noqa: BLE001 - Third-party lexer failures must leave the diff readable.
        return ""
