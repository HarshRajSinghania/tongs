"""Structured error hierarchy for forge operations."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any


class ForgeError(Exception):
    """Base error for all forge operations."""


class AuthError(ForgeError):
    """Authentication failed or missing. User should run glab/gh auth login."""


class RateLimitError(ForgeError):
    """Forge API rate limit exceeded. Retry after the specified delay."""

    def __init__(self, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class NetworkError(ForgeError):
    """Network connectivity issue. Switch to offline/cached mode."""


class NotFoundError(ForgeError):
    """Resource not found (404). MR may have been deleted or repo inaccessible."""


class ConflictError(ForgeError):
    """Operation conflicts with current state (e.g. merge conflicts, already merged)."""


class ForgePermissionError(ForgeError):
    """Insufficient permissions for the requested operation."""


class ValidationError(ForgeError):
    """Forge rejected the request as invalid before applying the mutation."""


class ContentTooLargeError(ForgeError):
    """A forge body is larger than the caller's byte limit.

    ``limit_bytes`` is the limit the body went past. The read stops at the
    limit, so the full size is not known.
    """

    def __init__(self, limit_bytes: int, message: str | None = None):
        text = (
            message
            if message is not None
            else f"The content is larger than {limit_bytes} bytes"
        )
        super().__init__(redact_credentials(text))
        self.limit_bytes = limit_bytes


class UnsupportedFileReadError(ForgeError):
    """This forge client cannot read a file at a commit.

    Raised by clients without a file read, and by a file read the forge
    answers with a redirect, which is never followed.
    """

    def __init__(self, message: str = "This forge cannot read files at a commit"):
        super().__init__(redact_credentials(message))


class ConfigError(ForgeError):
    """Configuration error (malformed config, invalid scan root, etc.)."""


class PartialResultsError(ForgeError):
    """A list read returned only some of its results.

    ``items`` holds the results that were read and ``cause`` the failure that
    left the rest out, for example a pull request that could not be fetched or
    a search that stopped at the forge's result cap.
    """

    def __init__(
        self,
        items: Iterable[Any],
        cause: ForgeError,
        message: str | None = None,
    ):
        super().__init__(message if message is not None else str(cause))
        self.items: tuple[Any, ...] = tuple(items)
        self.cause = cause


_TOKEN_PATTERNS = re.compile(
    r"("
    r"glpat-|gldt-|glcbt-\d*_?|glptt-|glft-|glsoat-|glimt-|gloas-|glrt-"
    r"|ghp_|gho_|ghs_|ghu_|ghr_|github_pat_"
    r"|Bearer\s+"
    r"|PRIVATE-TOKEN:\s*"
    r")[A-Za-z0-9_.\-]+"
)

# The userinfo part of a URL (``scheme://user:password@host``).
_URL_USERINFO = re.compile(r"(://)[^/?#\s]+@")


def redact_credentials(text: str) -> str:
    """Redact known token patterns and URL userinfo from text."""
    text = _URL_USERINFO.sub(r"\1[REDACTED]@", text)
    return _TOKEN_PATTERNS.sub(r"\1[REDACTED]", text)
