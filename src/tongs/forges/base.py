"""Abstract base class for forge client implementations."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from tongs.forges.models import (
    CIStatus,
    Commit,
    Discussion,
    ForgeMergeResult,
    ForgeMutationResult,
    MRDetail,
    MRPage,
    MRSummary,
    Pipeline,
    PipelineJob,
    ReviewDecision,
)


class ForgeClient(ABC):
    """Abstract interface for forge operations.

    Each concrete implementation (GitHub, GitLab) handles API differences
    internally and returns the shared data models.
    """

    @abstractmethod
    async def list_mrs(
        self,
        repo_path: str,
        state: str = "open",
        per_page: int = 100,
    ) -> list[MRSummary]: ...

    async def list_mrs_page(
        self,
        repo_path: str,
        state: str = "open",
        page: int = 1,
        per_page: int = 100,
        *,
        with_ci: bool = True,
    ) -> MRPage:
        """Read one page of reviews, most recently updated first.

        Forge clients override this with a single list request and CI lookups
        for that page only. A client whose ``supports_lazy_ci`` is true skips
        those lookups when ``with_ci`` is false: its rows then carry
        ``CIStatus.UNKNOWN`` and a ``head_commit_sha`` for
        ``get_mr_ci_status``. This fallback keeps third-party clients working by slicing the complete
        list, which already carries CI, so it ignores ``with_ci``.
        """
        if page < 1 or per_page < 1:
            raise ValueError("page and per_page must be positive")
        everything = await self.list_mrs(repo_path, state=state, per_page=per_page)
        ordered = sorted(everything, key=_newest_first_key, reverse=True)
        start = (page - 1) * per_page
        return MRPage(
            tuple(ordered[start : start + per_page]),
            has_next=len(ordered) > start + per_page,
        )

    async def get_mr_ci_status(
        self, repo_path: str, number: int, head_sha: str = ""
    ) -> CIStatus:
        """Read the CI status of one review's head commit.

        ``head_sha`` is the head the caller listed; a client may read the
        review to find it when it is empty. Forge clients override this. A
        client without a lookup reports UNKNOWN without contacting the forge.
        """
        return CIStatus.UNKNOWN

    async def get_mr_ci_status_fresh(
        self, repo_path: str, number: int, head_sha: str = ""
    ) -> CIStatus:
        """Read one review's CI status without a response-cache result."""
        return await self.get_mr_ci_status(repo_path, number, head_sha)

    @abstractmethod
    async def list_my_reviews(self) -> list[MRSummary]: ...

    @abstractmethod
    async def list_my_mrs(self) -> list[MRSummary]: ...

    @abstractmethod
    async def get_mr(self, repo_path: str, number: int) -> MRDetail: ...

    async def get_mr_fresh(self, repo_path: str, number: int) -> MRDetail:
        """Read MR detail without a response-cache result when supported."""
        return await self.get_mr(repo_path, number)

    async def get_mr_revision(self, repo_path: str, number: int) -> MRDetail:
        """Read the review resource alone, fresh, for its revision SHAs.

        Revision checks only need the head, base and start SHAs, so forge
        clients override this with the single pull or merge request read and
        skip the CI, review and approval requests of a full detail read. The
        result may then lack ``ci_status`` and ``approvals``, so it serves
        revision checks and never display. This fallback keeps third-party
        clients working with a full fresh read.
        """
        return await self.get_mr_fresh(repo_path, number)

    @abstractmethod
    async def get_mr_diff(self, repo_path: str, number: int) -> list[dict]: ...

    async def get_mr_diff_fresh(self, repo_path: str, number: int) -> list[dict]:
        """Read diff changes without a response-cache result when supported."""
        return await self.get_mr_diff(repo_path, number)

    @abstractmethod
    async def list_mr_commits(self, repo_path: str, number: int) -> list[Commit]: ...

    @abstractmethod
    async def get_mr_discussions(
        self, repo_path: str, number: int
    ) -> list[Discussion]: ...

    @abstractmethod
    async def create_inline_comment(
        self,
        repo_path: str,
        number: int,
        file_path: str,
        line: int,
        side: str,
        body: str,
        start_line: int | None = None,
        start_side: str | None = None,
        *,
        old_path: str | None = None,
        new_path: str | None = None,
        head_sha: str | None = None,
        base_sha: str | None = None,
        start_sha: str | None = None,
        old_line: int | None = None,
        new_line: int | None = None,
        start_old_line: int | None = None,
        start_new_line: int | None = None,
    ) -> ForgeMutationResult: ...

    @abstractmethod
    async def reply_to_discussion(
        self,
        repo_path: str,
        number: int,
        discussion_id: str,
        body: str,
        *,
        root_comment_id: str | None = None,
    ) -> ForgeMutationResult: ...

    @abstractmethod
    async def resolve_discussion(
        self,
        repo_path: str,
        number: int,
        discussion_id: str,
        resolved: bool,
    ) -> ForgeMutationResult: ...

    @abstractmethod
    async def submit_review(
        self,
        repo_path: str,
        number: int,
        verdict: ReviewDecision,
        body: str,
        inline_comments: list[dict] | None = None,
        *,
        head_sha: str | None = None,
    ) -> ForgeMutationResult: ...

    @abstractmethod
    async def approve_mr(
        self, repo_path: str, number: int, *, head_sha: str | None = None
    ) -> ForgeMutationResult: ...

    async def unapprove_mr(self, repo_path: str, number: int) -> ForgeMutationResult:
        raise NotImplementedError("This forge does not support unapprove")

    @abstractmethod
    async def merge_mr(
        self,
        repo_path: str,
        number: int,
        squash: bool = False,
        delete_branch: bool = True,
        *,
        head_sha: str | None = None,
        expected_source_repository: str | None = None,
        expected_source_branch: str | None = None,
        expected_target_branch: str | None = None,
    ) -> ForgeMergeResult: ...

    @abstractmethod
    async def close_mr(self, repo_path: str, number: int) -> ForgeMutationResult: ...

    @abstractmethod
    async def reopen_mr(self, repo_path: str, number: int) -> ForgeMutationResult: ...

    @abstractmethod
    async def add_comment(
        self, repo_path: str, number: int, body: str
    ) -> ForgeMutationResult: ...

    async def invalidate_review_reads(self, repo_path: str, number: int) -> bool:
        """Invalidate cached review reads when this client owns a cache."""
        return True

    async def invalidate_repository_reads(self, repo_path: str) -> bool:
        """Drop cached reads of one repository when this client owns a cache.

        Returns whether cached entries were dropped. A client without a cache
        has nothing to drop and returns False; its next read goes to the forge.
        """
        return False

    @abstractmethod
    async def list_pipelines(
        self,
        repo_path: str,
        per_page: int = 20,
    ) -> list[Pipeline]: ...

    @abstractmethod
    async def get_pipeline_jobs(
        self, repo_path: str, pipeline_id: int
    ) -> list[PipelineJob]: ...

    @abstractmethod
    async def get_job_log(self, repo_path: str, job_id: int) -> str: ...

    async def stream_job_log(self, repo_path: str, job_id: int) -> AsyncIterator[str]:
        """Stream job log lines. Default: return full log as single yield."""
        yield await self.get_job_log(repo_path, job_id)

    @abstractmethod
    async def retry_job(self, repo_path: str, job_id: int) -> None: ...

    @abstractmethod
    async def cancel_pipeline(self, repo_path: str, pipeline_id: int) -> None: ...

    async def list_mr_pipelines(
        self, repo_path: str, number: int, per_page: int = 20
    ) -> list[Pipeline]:
        """List pipelines for a specific MR/PR. Default: falls back to list_pipelines."""
        return await self.list_pipelines(repo_path, per_page=per_page)

    async def retry_pipeline(self, repo_path: str, pipeline_id: int) -> None:
        """Retry all failed jobs in a pipeline."""
        raise NotImplementedError("This forge does not support retry_pipeline")

    async def cancel_job(self, repo_path: str, job_id: int) -> None:
        """Cancel a single job."""
        raise NotImplementedError("This forge does not support cancel_job")

    @property
    def supports_job_cancel(self) -> bool:
        return False

    @property
    def supports_lazy_ci(self) -> bool:
        """Whether ``list_mrs_page`` honors ``with_ci=False``."""
        return False

    @property
    def supports_batched_review(self) -> bool:
        return False

    @property
    def supports_thread_resolution(self) -> bool:
        return False

    @property
    def supports_draft_notes(self) -> bool:
        return False

    @property
    def supports_unapprove(self) -> bool:
        return False

    @abstractmethod
    async def close(self) -> None:
        """Close the underlying HTTP client."""
        ...


def _newest_first_key(item: MRSummary) -> tuple[bool, datetime]:
    """Sort key for newest first that puts items without a time last.

    A third-party client may leave ``updated_at`` unset; such items keep their
    list order after every dated one instead of failing the whole page.
    """
    updated = getattr(item, "updated_at", None)
    if isinstance(updated, datetime):
        return True, updated
    return False, datetime.min.replace(tzinfo=UTC)


async def read_mr_page(
    client: ForgeClient,
    repo_path: str,
    state: str,
    page: int,
    per_page: int,
    *,
    with_ci: bool = True,
) -> MRPage:
    """Read one review page from any client, including ones without paging.

    A client that does not define ``list_mrs_page`` (for example a duck-typed
    test double or an older plugin) gets the slicing fallback of the base class.
    ``with_ci=False`` reaches only a client that declares ``supports_lazy_ci``;
    any other client returns its page with CI as before.
    """
    if getattr(type(client), "list_mrs_page", None) is None:
        return await ForgeClient.list_mrs_page(
            client, repo_path, state=state, page=page, per_page=per_page
        )
    if not with_ci and getattr(client, "supports_lazy_ci", False) is True:
        return await client.list_mrs_page(
            repo_path, state=state, page=page, per_page=per_page, with_ci=False
        )
    return await client.list_mrs_page(
        repo_path, state=state, page=page, per_page=per_page
    )


async def read_mr_ci_status(
    client: ForgeClient,
    repo_path: str,
    number: int,
    head_sha: str,
    *,
    fresh: bool = False,
) -> CIStatus:
    """Read one review's CI status from any client.

    A client without the lookup (a duck-typed test double or an older plugin)
    reports UNKNOWN without contacting the forge.
    """
    name = "get_mr_ci_status_fresh" if fresh else "get_mr_ci_status"
    lookup = getattr(client, name, None)
    if lookup is None and fresh:
        lookup = getattr(client, "get_mr_ci_status", None)
    if lookup is None:
        return CIStatus.UNKNOWN
    return await lookup(repo_path, number, head_sha)


async def read_mr_revision(
    client: ForgeClient, repo_path: str, number: int
) -> MRDetail:
    """Read one review's revision-only detail from any client.

    A client without ``get_mr_revision`` (a duck-typed test double or an older
    plugin) falls back to its fresh full detail read.
    """
    lookup = getattr(client, "get_mr_revision", None)
    if lookup is None:
        return await client.get_mr_fresh(repo_path, number)
    return await lookup(repo_path, number)
