"""Caching wrapper for ForgeClient that intercepts read methods."""

from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import asdict, replace
from datetime import datetime
from typing import Any

from tongs.cache.store import CacheStore
from tongs.config import DEFAULT_FILE_CONTENT_TTL
from tongs.errors import UnsupportedFileReadError
from tongs.forges.base import (
    ForgeClient,
    read_mr_ci_status,
    read_mr_page,
    read_mr_revision,
)
from tongs.forges.models import (
    CIStatus,
    ForgeMergeResult,
    ForgeMutationResult,
    MRDetail,
    MRPage,
    MRState,
    MRSummary,
    ReviewDecision,
    User,
)

_MAX_BACKGROUND_INVALIDATIONS = 64

#: The default time a file body read at a commit stays cached, in seconds.
FILE_CONTENT_TTL = int(DEFAULT_FILE_CONTENT_TTL.total_seconds())
#: The largest file body kept in the cache database. Larger bodies are
#: returned to the caller and never stored.
FILE_CACHE_MAX_BYTES = 1024 * 1024
# A full commit SHA (SHA-1 or SHA-256). Only these refs name immutable content.
_FULL_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_BACKGROUND_CLOSE_TIMEOUT = 1.0


def _finish_background_invalidation(
    tasks: dict[tuple[str, ...], asyncio.Task[bool]],
    key: tuple[str, ...],
    task: asyncio.Task[bool],
) -> None:
    if tasks.get(key) is task:
        del tasks[key]
    try:
        task.result()
    except (asyncio.CancelledError, Exception):  # noqa: BLE001, S110
        pass


def _key_under(key: str, prefix: str) -> bool:
    """Return whether ``key`` is ``prefix`` or below it across a ':' boundary.

    This matches what ``CacheStore.invalidate_prefix`` removes: MR 1 does not
    cover MR 12, and ``o/r`` does not cover ``o/r-docs``. Like the store, the
    comparison ignores case.
    """
    key = key.casefold()
    stem = prefix.casefold().removesuffix(":")
    return key == stem or key.startswith(f"{stem}:")


def _serialize_datetime(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _parse_datetime(val: str | None) -> datetime | None:
    if not val:
        return None
    return datetime.fromisoformat(val)


def _mr_summary_to_dict(mr: MRSummary) -> dict:
    d = asdict(mr)
    d["ci_status"] = mr.ci_status.value
    d["state"] = mr.state.value
    d["created_at"] = _serialize_datetime(mr.created_at)
    d["updated_at"] = _serialize_datetime(mr.updated_at)
    d["forge_host"] = {
        "hostname": mr.forge_host.hostname,
        "forge_type": mr.forge_host.forge_type.value,
        "api_base": mr.forge_host.api_base,
    }
    d["author"] = asdict(mr.author)
    d["labels"] = list(mr.labels)
    d["review_decision"] = (
        mr.review_decision.value if mr.review_decision is not None else None
    )
    return d


def _dict_to_mr_summary(d: dict) -> MRSummary:
    from tongs.forges.models import ForgeHost
    from tongs.scanner.repo import ForgeType

    return MRSummary(
        number=d["number"],
        title=d["title"],
        author=User(**d["author"]),
        source_branch=d["source_branch"],
        target_branch=d["target_branch"],
        state=MRState(d["state"]),
        ci_status=CIStatus(d["ci_status"]),
        web_url=d["web_url"],
        repo_path=d["repo_path"],
        local_path=d.get("local_path", ""),
        is_draft=d.get("is_draft", False),
        forge_host=ForgeHost(
            hostname=d["forge_host"]["hostname"],
            forge_type=ForgeType(d["forge_host"]["forge_type"]),
            api_base=d["forge_host"]["api_base"],
        ),
        created_at=_parse_datetime(d.get("created_at")),
        updated_at=_parse_datetime(d.get("updated_at")),
        comment_count=d.get("comment_count", 0),
        has_conflicts=d.get("has_conflicts", False),
        labels=tuple(d.get("labels", ())),
        review_decision=(
            ReviewDecision(d["review_decision"]) if d.get("review_decision") else None
        ),
        additions=d.get("additions"),
        deletions=d.get("deletions"),
        # Rows cached before the head commit was listed have none.
        head_commit_sha=(
            d["head_commit_sha"] if isinstance(d.get("head_commit_sha"), str) else ""
        ),
    )


class CachedForgeClient:
    """Wraps a ForgeClient with SQLite caching on read methods.

    Mutations bypass the cache and invalidate related entries.
    Job logs are never cached (security: may contain secrets).
    """

    def __init__(
        self,
        inner: ForgeClient,
        cache: CacheStore,
        hostname: str,
        mr_list_ttl: int = 60,
        diff_ttl: int = 300,
        file_content_ttl: int = FILE_CONTENT_TTL,
    ) -> None:
        self._inner = inner
        self._cache = cache
        self._hostname = hostname
        self._mr_list_ttl = mr_list_ttl
        self._diff_ttl = diff_ttl
        self._file_content_ttl = file_content_ttl
        self._dirty_review_prefixes: dict[str, int] = {}
        self._dirty_generation = 0
        # The latest generation that marked each prefix dirty, kept after the
        # invalidation finishes so a read can tell whether one covered it.
        # Only marks newer than the oldest read in flight are kept.
        self._marked_generations: dict[str, int] = {}
        # How many uncached forge reads are in flight for each generation.
        self._read_generations: dict[int, int] = {}
        self._invalidation_tasks: dict[tuple[str, ...], asyncio.Task[bool]] = {}
        self._closed = False

    def _key(self, *parts: str | int) -> str:
        return f"{self._hostname}:{':'.join(str(p) for p in parts)}"

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def _is_dirty(self, key: str) -> bool:
        return any(_key_under(key, prefix) for prefix in self._dirty_review_prefixes)

    def _may_store(self, key: str, generation: int) -> bool:
        """Return whether no invalidation covering ``key`` began after ``generation``.

        ``generation`` is ``_dirty_generation`` read before the forge read. A
        mutation that lands while that read is in flight may make its result
        older than the mutation, so the result is then not stored. Invalidations
        of other repositories and reviews do not count.
        """
        if self._dirty_generation == generation:
            return True
        return not any(
            marked > generation and _key_under(key, prefix)
            for prefix, marked in self._marked_generations.items()
        )

    def _begin_read(self) -> int:
        """Record a forge read starting now and return its generation."""
        generation = self._dirty_generation
        self._read_generations[generation] = (
            self._read_generations.get(generation, 0) + 1
        )
        return generation

    def _end_read(self, generation: int) -> None:
        """Record a forge read finishing and drop marks no read still needs."""
        remaining = self._read_generations[generation] - 1
        if remaining:
            self._read_generations[generation] = remaining
        else:
            del self._read_generations[generation]
        self._prune_marked_generations()

    def _prune_marked_generations(self) -> None:
        """Forget marks that no read in flight or started later can be older than.

        ``_may_store`` only consults a mark newer than the read's generation. A
        read starting later takes the current generation, which no mark exceeds,
        so only the oldest read in flight bounds which marks still matter.
        """
        floor = min(self._read_generations, default=self._dirty_generation)
        stale = [
            prefix
            for prefix, marked in self._marked_generations.items()
            if marked <= floor
        ]
        for prefix in stale:
            del self._marked_generations[prefix]

    # -- Cached reads --

    async def list_mrs(
        self, repo_path: str, state: str = "open", per_page: int = 100
    ) -> list[MRSummary]:
        key = self._key(repo_path, "mrs", state)
        if self._is_dirty(key):
            return await self._inner.list_mrs(repo_path, state, per_page)
        cached = await self._cache.get_json(key)
        if cached is not None:
            return [_dict_to_mr_summary(d) for d in cached]
        generation = self._begin_read()
        try:
            result = await self._inner.list_mrs(repo_path, state, per_page)
            if self._may_store(key, generation):
                await self._cache.put_json(
                    key, [_mr_summary_to_dict(mr) for mr in result], self._mr_list_ttl
                )
        finally:
            self._end_read(generation)
        return result

    async def list_mrs_page(
        self,
        repo_path: str,
        state: str = "open",
        page: int = 1,
        per_page: int = 100,
        *,
        with_ci: bool = True,
    ) -> MRPage:
        """Cache the first page only, below the repository list prefix.

        Offset pages are not stable over time: a review updated between two
        reads moves up a page. A later page is therefore always read from the
        forge, and reading one drops the cached first pages, so the next read
        of this repository never starts from a first page older than the
        later pages already shown. A first page read without CI is cached
        apart from one read with it, so neither stands in for the other.
        """
        lazy = not with_ci and self.supports_lazy_ci
        key = self._first_page_key(repo_path, state, per_page, lazy=lazy)

        async def read() -> MRPage:
            return await read_mr_page(
                self._inner, repo_path, state, page, per_page, with_ci=not lazy
            )

        if page != 1:
            for first_page in (False, True):
                await self._cache.invalidate(
                    self._first_page_key(repo_path, state, per_page, lazy=first_page)
                )
            return await read()
        if self._is_dirty(key):
            return await read()
        cached = await self._cache.get_json(key)
        if isinstance(cached, dict) and isinstance(cached.get("items"), list):
            return MRPage(
                tuple(_dict_to_mr_summary(d) for d in cached["items"]),
                has_next=bool(cached.get("has_next")),
            )
        generation = self._begin_read()
        try:
            result = await read()
            if self._may_store(key, generation):
                await self._cache.put_json(
                    key,
                    {
                        "items": [_mr_summary_to_dict(mr) for mr in result.items],
                        "has_next": result.has_next,
                    },
                    self._mr_list_ttl,
                )
        finally:
            self._end_read(generation)
        return result

    @property
    def supports_lazy_ci(self) -> bool:
        return getattr(self._inner, "supports_lazy_ci", False) is True

    def _first_page_key(
        self, repo_path: str, state: str, per_page: int, *, lazy: bool
    ) -> str:
        parts: tuple[str | int, ...] = (repo_path, "mrs", state, "page", per_page, 1)
        return self._key(*parts, "noci") if lazy else self._key(*parts)

    async def get_mr_ci_status(
        self, repo_path: str, number: int, head_sha: str = ""
    ) -> CIStatus:
        """Cache one review's CI status for the review list lifetime.

        The entry sits below the repository list prefix, so everything that
        drops cached review lists (a refresh, a review write, a CI retry or
        cancel) drops it too, and a new head commit has its own entry.
        """
        key = self._key(repo_path, "mrs", "ci", number, head_sha)
        if not self._is_dirty(key):
            cached = await self._cache.get_json(key)
            if isinstance(cached, dict) and isinstance(cached.get("status"), str):
                try:
                    return CIStatus(cached["status"])
                except ValueError:
                    pass
        return await self._read_ci_status(key, repo_path, number, head_sha)

    async def get_mr_ci_status_fresh(
        self, repo_path: str, number: int, head_sha: str = ""
    ) -> CIStatus:
        """Read past a cached CI status and store what the forge returns."""
        key = self._key(repo_path, "mrs", "ci", number, head_sha)
        return await self._read_ci_status(key, repo_path, number, head_sha)

    async def _read_ci_status(
        self, key: str, repo_path: str, number: int, head_sha: str
    ) -> CIStatus:
        if self._is_dirty(key):
            return await read_mr_ci_status(self._inner, repo_path, number, head_sha)
        generation = self._begin_read()
        try:
            result = await read_mr_ci_status(self._inner, repo_path, number, head_sha)
            if isinstance(result, CIStatus) and self._may_store(key, generation):
                await self._cache.put_json(
                    key, {"status": result.value}, self._mr_list_ttl
                )
        finally:
            self._end_read(generation)
        return result

    async def get_mr_diff(self, repo_path: str, number: int) -> list[dict]:
        key = self._key(repo_path, "mr", number, "diff")
        if self._is_dirty(key):
            return await self._inner.get_mr_diff(repo_path, number)
        cached = await self._cache.get_json(key)
        if cached is not None:
            return cached
        generation = self._begin_read()
        try:
            result = await self._inner.get_mr_diff(repo_path, number)
            if self._may_store(key, generation):
                await self._cache.put_json(key, result, self._diff_ttl)
        finally:
            self._end_read(generation)
        return result

    async def get_mr_fresh(self, repo_path: str, number: int) -> MRDetail:
        """Bypass any present or future wrapper cache for revision checks."""
        return await self._inner.get_mr_fresh(repo_path, number)

    async def get_mr_revision(self, repo_path: str, number: int) -> MRDetail:
        """Read the revision-only review resource, never from the cache."""
        return await read_mr_revision(self._inner, repo_path, number)

    async def get_mr_diff_fresh(self, repo_path: str, number: int) -> list[dict]:
        """Bypass cached diff payloads for revision-bound reads."""
        return await self._inner.get_mr_diff_fresh(repo_path, number)

    async def get_file_at_ref(
        self, repo_path: str, path: str, ref: str, *, max_bytes: int
    ) -> bytes:
        """Read one file at a commit, keeping bodies of up to 1 MiB.

        Content at a full commit SHA never changes, so no mutation drops it;
        clearing the cache or the repository's cached reads does. Any other
        ref is read from the forge every time. The path is hashed in the key,
        because store prefix matching ignores case and a path may hold ':'.
        A cached body longer than ``max_bytes`` counts as a miss, so the
        forge read applies the caller's limit. Errors are never cached.
        """
        lookup = getattr(self._inner, "get_file_at_ref", None)
        if lookup is None:
            raise UnsupportedFileReadError()
        if _FULL_SHA.fullmatch(ref) is None:
            return await lookup(repo_path, path, ref, max_bytes=max_bytes)
        digest = hashlib.sha256(path.encode("utf-8", "surrogatepass")).hexdigest()
        key = self._key(repo_path, "file", ref, digest)
        if self._is_dirty(key):
            return await lookup(repo_path, path, ref, max_bytes=max_bytes)
        cached = await self._cache.get(key)
        if cached is not None and len(cached) <= max_bytes:
            return cached
        generation = self._begin_read()
        try:
            body = await lookup(repo_path, path, ref, max_bytes=max_bytes)
            if (
                isinstance(body, bytes)
                and len(body) <= FILE_CACHE_MAX_BYTES
                and self._may_store(key, generation)
            ):
                await self._cache.put(key, body, self._file_content_ttl)
        finally:
            self._end_read(generation)
        return body

    # -- Mutations that invalidate cache --

    async def approve_mr(
        self, repo_path: str, number: int, *, head_sha: str | None = None
    ) -> ForgeMutationResult:
        if head_sha is None:
            result = await self._inner.approve_mr(repo_path, number)
        else:
            result = await self._inner.approve_mr(repo_path, number, head_sha=head_sha)
        return await self._finish_review_mutation(repo_path, number, result)

    async def unapprove_mr(self, repo_path: str, number: int) -> ForgeMutationResult:
        result = await self._inner.unapprove_mr(repo_path, number)
        return await self._finish_review_mutation(repo_path, number, result)

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
    ) -> ForgeMergeResult:
        if all(
            item is None
            for item in (
                head_sha,
                expected_source_repository,
                expected_source_branch,
                expected_target_branch,
            )
        ):
            result = await self._inner.merge_mr(
                repo_path, number, squash, delete_branch
            )
        else:
            result = await self._inner.merge_mr(
                repo_path,
                number,
                squash,
                delete_branch,
                head_sha=head_sha,
                expected_source_repository=expected_source_repository,
                expected_source_branch=expected_source_branch,
                expected_target_branch=expected_target_branch,
            )
        prefixes = (self._key(repo_path),)
        self._mark_review_dirty(prefixes)
        self._schedule_review_invalidation(prefixes)
        return replace(result, cache_invalidated=False)

    async def close_mr(self, repo_path: str, number: int) -> ForgeMutationResult:
        result = await self._inner.close_mr(repo_path, number)
        return await self._finish_review_mutation(repo_path, number, result)

    async def reopen_mr(self, repo_path: str, number: int) -> ForgeMutationResult:
        result = await self._inner.reopen_mr(repo_path, number)
        return await self._finish_review_mutation(repo_path, number, result)

    async def add_comment(
        self, repo_path: str, number: int, body: str
    ) -> ForgeMutationResult:
        result = await self._inner.add_comment(repo_path, number, body)
        return await self._finish_review_mutation(repo_path, number, result)

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
        **revision: Any,
    ) -> ForgeMutationResult:
        result = await self._inner.create_inline_comment(
            repo_path,
            number,
            file_path,
            line,
            side,
            body,
            start_line,
            start_side,
            **revision,
        )
        return await self._finish_review_mutation(repo_path, number, result)

    async def reply_to_discussion(
        self,
        repo_path: str,
        number: int,
        discussion_id: str,
        body: str,
        *,
        root_comment_id: str | None = None,
    ) -> ForgeMutationResult:
        if root_comment_id is None:
            result = await self._inner.reply_to_discussion(
                repo_path, number, discussion_id, body
            )
        else:
            result = await self._inner.reply_to_discussion(
                repo_path,
                number,
                discussion_id,
                body,
                root_comment_id=root_comment_id,
            )
        return await self._finish_review_mutation(repo_path, number, result)

    async def resolve_discussion(
        self, repo_path: str, number: int, discussion_id: str, resolved: bool
    ) -> ForgeMutationResult:
        result = await self._inner.resolve_discussion(
            repo_path, number, discussion_id, resolved
        )
        return await self._finish_review_mutation(repo_path, number, result)

    async def submit_review(
        self,
        repo_path: str,
        number: int,
        verdict: Any,
        body: str,
        inline_comments: list[dict] | None = None,
        *,
        head_sha: str | None = None,
    ) -> ForgeMutationResult:
        if head_sha is None:
            result = await self._inner.submit_review(
                repo_path, number, verdict, body, inline_comments
            )
        else:
            result = await self._inner.submit_review(
                repo_path,
                number,
                verdict,
                body,
                inline_comments,
                head_sha=head_sha,
            )
        return await self._finish_review_mutation(repo_path, number, result)

    async def invalidate_review_reads(self, repo_path: str, number: int) -> bool:
        prefixes = self._review_prefixes(repo_path, number)
        generation = self._mark_review_dirty(prefixes)
        return await self._invalidate_marked(prefixes, generation)

    async def invalidate_repository_reads(self, repo_path: str) -> bool:
        """Drop every cached read of one repository, such as its review lists.

        Returns whether the stored entries are gone. Until they are, reads of
        the repository in this process bypass the cache and store nothing, and
        a read already in flight never stores its older result.
        """
        prefixes = (self._key(repo_path),)
        generation = self._mark_review_dirty(prefixes)
        return await self._invalidate_marked(prefixes, generation)

    async def invalidate_review_list_reads(self, repo_path: str) -> bool:
        """Drop the cached review lists and first pages of one repository.

        Cached diffs and other per-review reads stay. Returns whether the
        stored entries are gone, with the same in-process guarantees as
        ``invalidate_repository_reads`` until they are.
        """
        prefixes = (self._key(repo_path, "mrs"),)
        generation = self._mark_review_dirty(prefixes)
        return await self._invalidate_marked(prefixes, generation)

    async def _invalidate_marked(
        self, prefixes: tuple[str, ...], generation: int
    ) -> bool:
        results = await asyncio.gather(
            *(self._cache.invalidate_prefix(prefix) for prefix in prefixes),
            return_exceptions=True,
        )
        complete = not any(isinstance(result, BaseException) for result in results)
        if complete:
            for prefix in prefixes:
                if self._dirty_review_prefixes.get(prefix) == generation:
                    del self._dirty_review_prefixes[prefix]
        return complete

    async def _finish_review_mutation(
        self, repo_path: str, number: int, result: ForgeMutationResult
    ) -> ForgeMutationResult:
        prefixes = self._review_prefixes(repo_path, number)
        self._mark_review_dirty(prefixes)
        self._schedule_review_invalidation(prefixes)
        return replace(result, cache_invalidated=False)

    def _schedule_review_invalidation(self, prefixes: tuple[str, ...]) -> None:
        key = prefixes
        active = self._invalidation_tasks.get(key)
        if self._closed or (active is not None and not active.done()):
            return
        if len(self._invalidation_tasks) >= _MAX_BACKGROUND_INVALIDATIONS:
            return
        task = asyncio.create_task(self._drain_review_invalidation(prefixes))
        self._invalidation_tasks[key] = task
        task.add_done_callback(
            lambda completed: _finish_background_invalidation(
                self._invalidation_tasks, key, completed
            )
        )

    async def _drain_review_invalidation(self, prefixes: tuple[str, ...]) -> bool:
        while not self._closed:
            generations = [
                self._dirty_review_prefixes.get(prefix) for prefix in prefixes
            ]
            if any(generation is None for generation in generations):
                return True
            generation = max(value for value in generations if value is not None)
            if not await self._invalidate_marked(prefixes, generation):
                return False
            if not any(prefix in self._dirty_review_prefixes for prefix in prefixes):
                return True
        return False

    def _mark_review_dirty(self, prefixes: tuple[str, ...]) -> int:
        self._dirty_generation += 1
        for prefix in prefixes:
            self._dirty_review_prefixes[prefix] = self._dirty_generation
            self._marked_generations[prefix] = self._dirty_generation
        self._prune_marked_generations()
        return self._dirty_generation

    def _review_prefixes(self, repo_path: str, number: int) -> tuple[str, str]:
        return (
            self._key(repo_path, "mrs"),
            self._key(repo_path, "mr", number),
        )

    async def close(self) -> None:
        self._closed = True
        pending = set(self._invalidation_tasks.values())
        for _attempt in range(2):
            if not pending:
                break
            for task in pending:
                task.cancel()
            _done, pending = await asyncio.wait(
                pending, timeout=_BACKGROUND_CLOSE_TIMEOUT
            )
        if pending:
            raise RuntimeError("cache invalidation tasks did not stop")
        await self._inner.close()
