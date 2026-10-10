"""MCP server exposing tongs forge operations as tools.

Entry point: tongs-mcp
Tools are high-level only. Destructive actions (merge, close, reopen,
cancel) are intentionally excluded as a security boundary.

This module is the only one that touches the MCP SDK: it registers the tools,
owns the server's application session for its lifetime, and turns a
:class:`~tongs.mcp.tools.ToolFailure` into the SDK's ``ToolError`` so clients
read the message. The tool logic lives in :mod:`tongs.mcp.tools`.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from tongs.mcp import tools
from tongs.mcp.tools import DEFAULT_LIST_LIMIT, SessionHolder, ToolFailure
from tongs.services.errors import ServiceError
from tongs.services.session import ApplicationSession

log = logging.getLogger(__name__)

# The one application session this process uses, started on the first call.
sessions = SessionHolder()


@asynccontextmanager
async def lifespan(_server: FastMCP) -> AsyncIterator[None]:
    """Close the application session when the server shuts down."""
    try:
        yield
    finally:
        with anyio.CancelScope(shield=True):
            try:
                await sessions.close()
            except ServiceError:
                log.warning("The tongs-mcp application session did not close cleanly")


mcp = FastMCP("tongs", lifespan=lifespan)


async def _run[ResultT](
    operation: str,
    call: Callable[[ApplicationSession], Awaitable[ResultT]],
) -> ResultT:
    try:
        session = await tools.open_session(sessions, operation)
        return await call(session)
    except ToolFailure as failure:
        raise ToolError(failure.message) from None


@mcp.tool()
async def list_mrs(
    repo_path: str, state: str = "open", limit: int = DEFAULT_LIST_LIMIT
) -> list[dict]:
    """List merge requests for a repository, most recently updated first.

    Reads one page of at most `limit` reviews. Open reviews include their CI
    status; closed and merged ones report it as unknown when the forge list
    does not carry it.

    Args:
        repo_path: Repository path as 'hostname/owner/repo' (e.g., 'github.com/acme/app')
        state: MR state filter: 'open' (default), 'closed' or 'merged'
        limit: Maximum number of reviews to return, 1 to 100 (default 30)
    """
    return await _run(
        "list_mrs", lambda session: tools.list_mrs(session, repo_path, state, limit)
    )


@mcp.tool()
async def get_mr(repo_path: str, number: int) -> dict:
    """Get detailed information about a merge request.

    The result includes head_sha, which approve_mr requires.

    Args:
        repo_path: Repository path as 'hostname/owner/repo'
        number: MR/PR number
    """
    return await _run(
        "get_mr", lambda session: tools.get_mr(session, repo_path, number)
    )


@mcp.tool()
async def get_mr_diff(repo_path: str, number: int) -> str:
    """Get the diff for a merge request as unified diff text.

    Every changed file appears. A file without a patch gets a marker such as
    [binary], [renamed] or [truncated]. Output stops at 256 KiB with a note
    naming how many files were left out.

    Args:
        repo_path: Repository path as 'hostname/owner/repo'
        number: MR/PR number
    """
    return await _run(
        "get_mr_diff", lambda session: tools.get_mr_diff(session, repo_path, number)
    )


@mcp.tool()
async def post_comment(repo_path: str, number: int, body: str) -> str:
    """Post a general comment on a merge request.

    Args:
        repo_path: Repository path as 'hostname/owner/repo'
        number: MR/PR number
        body: Comment text (markdown supported)
    """
    return await _run(
        "post_comment",
        lambda session: tools.post_comment(session, repo_path, number, body),
    )


@mcp.tool()
async def approve_mr(repo_path: str, number: int, head_sha: str = "") -> str:
    """Approve a merge request at the head commit you reviewed.

    head_sha is required: call get_mr first and pass the head_sha it
    returned. The approval fails if the review has new commits since then.

    Args:
        repo_path: Repository path as 'hostname/owner/repo'
        number: MR/PR number
        head_sha: Required. The head_sha that get_mr returned
    """
    return await _run(
        "approve_mr",
        lambda session: tools.approve_mr(session, repo_path, number, head_sha),
    )


@mcp.tool()
async def list_pipelines(repo_path: str, number: int) -> list[dict]:
    """List the 20 most recent pipelines/CI runs for a merge request.

    Args:
        repo_path: Repository path as 'hostname/owner/repo'
        number: MR/PR number
    """
    return await _run(
        "list_pipelines",
        lambda session: tools.list_pipelines(session, repo_path, number),
    )


def main() -> None:
    """Entry point for tongs-mcp."""
    mcp.run()


if __name__ == "__main__":
    main()
