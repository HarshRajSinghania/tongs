"""Bounded, validated reads of one review file at a commit.

The terminal and the desktop sidecar both read file content through this
service, so the input rules, the size limits and the outcome wording are the
same in both apps. Expected outcomes (too large, binary, missing, unsupported)
come back as ``FileRead`` values; authentication, rate-limit and network
failures still raise ``ServiceError``.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from tongs.errors import ContentTooLargeError, NotFoundError, UnsupportedFileReadError
from tongs.forges.base import ForgeClient, read_file_at_ref
from tongs.services.errors import ServiceError, ServiceErrorCode, translate_error
from tongs.services.models import ReviewRef

_MIB = 1024 * 1024

#: The longest accepted file path, in UTF-8 bytes.
MAX_PATH_BYTES = 4096
#: How many leading bytes are checked for a NUL, as git does, to call a file
#: binary.
BINARY_SNIFF_BYTES = 8000
#: The most decoded file bytes the in-memory cache keeps across all files.
MEMORY_CACHE_BYTES = 32 * _MIB
# Bytes counted for each memory cache entry on top of its body, so many empty
# files cannot grow the cache without bound.
_ENTRY_OVERHEAD_BYTES = 256

_FULL_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
# NUL, the other C0 control characters and DEL.
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


class FileReadStatus(StrEnum):
    """The outcome of one file read."""

    OK = "ok"
    TOO_LARGE = "too_large"
    BINARY = "binary"
    NOT_FOUND = "not_found"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class FileText:
    """A text file split into lines.

    ``lines`` holds the file split on ``"\\n"`` only, so a ``"\\r"`` stays in
    its line's content, as in the diff parser. The empty element after a final
    newline is dropped and recorded as ``ends_with_newline``. An empty file has
    no lines and counts as ending with a newline, because it has no last line
    without one. ``byte_size`` is the size of the body the forge returned.
    """

    lines: tuple[str, ...]
    ends_with_newline: bool
    byte_size: int


@dataclass(frozen=True, slots=True)
class FileRead:
    """One file read: its status, its text when OK, and a message otherwise."""

    status: FileReadStatus
    text: FileText | None
    message: str


@dataclass(frozen=True, slots=True)
class FileReadLimits:
    """The most bytes and lines a file read accepts."""

    max_bytes: int
    max_lines: int

    def __post_init__(self) -> None:
        if self.max_bytes <= 0 or self.max_lines <= 0:
            raise ValueError("file read limits must be positive")


#: Limits for expanding diff context in a review.
EXPAND_LIMITS = FileReadLimits(max_bytes=2 * _MIB, max_lines=20_000)
#: Limits for reading one side of a diff that is generated locally.
GENERATE_LIMITS = FileReadLimits(max_bytes=5 * _MIB, max_lines=100_000)

BINARY_MESSAGE = "This file is binary at this commit."
NOT_FOUND_MESSAGE = (
    "The forge has no copy of this file at this commit. Refresh the review."
)
UNSUPPORTED_MESSAGE = "This forge can't read files at a commit."


class ForgeCall(Protocol):
    """Run one forge awaitable and translate its failure, as the session does."""

    def __call__[T](
        self, awaitable: Awaitable[T], *, operation: str, hostname: str
    ) -> Awaitable[T]: ...


async def _translate_call[T](
    awaitable: Awaitable[T], *, operation: str, hostname: str
) -> T:
    try:
        return await awaitable
    except ServiceError:
        raise
    except Exception as error:  # noqa: BLE001 - Sanitize forge boundary.
        raise translate_error(error, operation=operation, hostname=hostname) from None


class ReviewFileService:
    """Read review files at a commit, bounded, validated and cached.

    ``get_client`` returns the forge client for an issued review, and ``call``
    runs each forge read so the session records rate limits and translates
    failures. A byte-bounded in-memory cache sits above the client's own
    response cache.
    """

    def __init__(
        self,
        *,
        get_client: Callable[[ReviewRef, str], Awaitable[ForgeClient]],
        call: ForgeCall = _translate_call,
        memory_cache_bytes: int = MEMORY_CACHE_BYTES,
    ) -> None:
        if memory_cache_bytes < 0:
            raise ValueError("memory_cache_bytes must not be negative")
        self._get_client = get_client
        self._call = call
        self._memory = _TextLRU(memory_cache_bytes)

    async def read_file(
        self,
        review: ReviewRef,
        path: str,
        ref: str,
        *,
        limits: FileReadLimits = EXPAND_LIMITS,
    ) -> FileRead:
        """Read ``path`` at commit ``ref`` of ``review``'s repository.

        ``ref`` must be a full lowercase commit SHA, and ``path`` a plain
        repository path; anything else raises ``ServiceError(INVALID_INPUT)``
        before any request. Too large, binary, missing and unsupported files
        come back as a status with a message.
        """
        validate_commit_sha(ref)
        validate_file_path(path)
        repository = review.repository
        # The client lookup also proves the session is open and issued this
        # review's repository, so a memory cache hit never skips those checks.
        client = await self._get_client(review, "read_review_file")
        key = (repository.hostname, repository.project_path, ref, path)
        cached = self._memory.get(key)
        if cached is not None:
            return _within_limits(cached, limits)

        async def fetch() -> bytes | FileReadStatus:
            try:
                return await read_file_at_ref(
                    client,
                    repository.project_path,
                    path,
                    ref,
                    max_bytes=limits.max_bytes,
                )
            except ContentTooLargeError:
                return FileReadStatus.TOO_LARGE
            except NotFoundError:
                return FileReadStatus.NOT_FOUND
            except UnsupportedFileReadError:
                return FileReadStatus.UNSUPPORTED

        result = await self._call(
            fetch(), operation="read_review_file", hostname=repository.hostname
        )
        if isinstance(result, FileReadStatus):
            return _failed(result, limits)
        if not isinstance(result, bytes):
            raise ServiceError(
                ServiceErrorCode.INVALID_RESPONSE,
                "The forge returned an invalid file.",
            )
        if len(result) > limits.max_bytes:
            return _failed(FileReadStatus.TOO_LARGE, limits)
        if b"\0" in result[:BINARY_SNIFF_BYTES]:
            return _failed(FileReadStatus.BINARY, limits)
        text = decode_file_text(result)
        if len(text.lines) > limits.max_lines:
            return _failed(FileReadStatus.TOO_LARGE, limits)
        self._memory.put(key, text)
        return FileRead(FileReadStatus.OK, text, "")

    def clear(self) -> None:
        """Forget every file kept in memory."""
        self._memory.clear()

    def close(self) -> None:
        """Release the in-memory cache when the session closes."""
        self._memory.clear()


def validate_commit_sha(ref: object) -> None:
    """Require a full lowercase SHA-1 or SHA-256 commit id."""
    if not isinstance(ref, str) or _FULL_SHA.fullmatch(ref) is None:
        raise ServiceError(
            ServiceErrorCode.INVALID_INPUT,
            "The commit must be a full commit SHA.",
        )


def validate_file_path(path: object) -> None:
    """Require a plain repository path before it goes into a forge URL.

    Git trees never hold an empty path, a control character, a leading
    ``/`` or an empty, ``.`` or ``..`` segment, so these rules only refuse
    paths that could change which URL is built.
    """
    if not isinstance(path, str) or not path:
        raise _invalid_path()
    try:
        encoded = path.encode("utf-8")
    except UnicodeEncodeError:
        raise _invalid_path() from None
    if len(encoded) > MAX_PATH_BYTES or _CONTROL_CHARACTERS.search(path):
        raise _invalid_path()
    if path.startswith("/"):
        raise _invalid_path()
    if any(segment in {"", ".", ".."} for segment in path.split("/")):
        raise _invalid_path()


def decode_file_text(data: bytes) -> FileText:
    """Decode a file body as UTF-8 and split it on ``"\\n"`` only.

    Invalid UTF-8 becomes U+FFFD. Other line separators, such as ``"\\r"`` or
    U+2028, stay in the line's content.
    """
    lines = data.decode("utf-8", errors="replace").split("\n")
    ends_with_newline = lines[-1] == ""
    if ends_with_newline:
        lines.pop()
    return FileText(tuple(lines), ends_with_newline, len(data))


def too_large_message(limits: FileReadLimits) -> str:
    """Describe the limits a file went past."""
    return (
        f"File too large to expand (over {_size_text(limits.max_bytes)} or "
        f"{limits.max_lines:,} lines)."
    )


def _size_text(size: int) -> str:
    if size % _MIB == 0:
        return f"{size // _MIB} MiB"
    if size % 1024 == 0:
        return f"{size // 1024} KiB"
    return f"{size:,} bytes"


def _within_limits(text: FileText, limits: FileReadLimits) -> FileRead:
    if text.byte_size > limits.max_bytes or len(text.lines) > limits.max_lines:
        return _failed(FileReadStatus.TOO_LARGE, limits)
    return FileRead(FileReadStatus.OK, text, "")


def _failed(status: FileReadStatus, limits: FileReadLimits) -> FileRead:
    messages = {
        FileReadStatus.BINARY: BINARY_MESSAGE,
        FileReadStatus.NOT_FOUND: NOT_FOUND_MESSAGE,
        FileReadStatus.UNSUPPORTED: UNSUPPORTED_MESSAGE,
    }
    message = messages.get(status) or too_large_message(limits)
    return FileRead(status, None, message)


def _invalid_path() -> ServiceError:
    return ServiceError(ServiceErrorCode.INVALID_INPUT, "The file path is invalid.")


_MemoryKey = tuple[str, str, str, str]


class _TextLRU:
    """Keep decoded files, least recently used first out, within a byte budget."""

    def __init__(self, max_bytes: int) -> None:
        self._max_bytes = max_bytes
        self._entries: OrderedDict[_MemoryKey, FileText] = OrderedDict()
        self._size = 0

    def get(self, key: _MemoryKey) -> FileText | None:
        text = self._entries.get(key)
        if text is not None:
            self._entries.move_to_end(key)
        return text

    def put(self, key: _MemoryKey, text: FileText) -> None:
        size = _entry_size(text)
        if size > self._max_bytes:
            return
        previous = self._entries.pop(key, None)
        if previous is not None:
            self._size -= _entry_size(previous)
        self._entries[key] = text
        self._size += size
        while self._size > self._max_bytes:
            _key, evicted = self._entries.popitem(last=False)
            self._size -= _entry_size(evicted)

    def clear(self) -> None:
        self._entries.clear()
        self._size = 0


def _entry_size(text: FileText) -> int:
    return text.byte_size + _ENTRY_OVERHEAD_BYTES


__all__ = [
    "EXPAND_LIMITS",
    "GENERATE_LIMITS",
    "FileRead",
    "FileReadLimits",
    "FileReadStatus",
    "FileText",
    "ReviewFileService",
    "decode_file_text",
    "validate_commit_sha",
    "validate_file_path",
]
