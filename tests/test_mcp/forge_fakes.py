"""A fake forge and session helpers for the tongs-mcp tool tests.

Every forge request goes to :class:`FakeForge` through a patched httpx
transport, so no test reaches the network.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import httpx

from tongs.config import Config, HostConfig
from tongs.services.session import ApplicationSession

HEAD_1 = "1" * 40
HEAD_2 = "2" * 40
BASE = "b" * 40
REPO = "github.com/acme/repo"

_PULL = re.compile(r"/repos/acme/repo/pulls/(\d+)")


def make_config(**overrides: object) -> Config:
    """A config with one extra host per forge instead of the user's file."""
    return Config(
        extra_hosts={
            "lab": HostConfig("git.lab.corp", "gitlab"),
            "hub": HostConfig("git.hub.corp", "github"),
        },
        **overrides,
    )


def make_session(tmp_path: Path, **config: object) -> ApplicationSession:
    """A session whose cache and draft database live under ``tmp_path``."""
    return ApplicationSession(
        config=make_config(**config),
        cache_path=tmp_path / "cache.db",
        draft_db_path=tmp_path / "drafts.db",
    )


def pull(number: int, *, state: str = "open", merged: bool = False) -> dict:
    return {
        "number": number,
        "title": f"PR {number}",
        "state": state,
        "merged_at": "2026-09-03T00:00:00Z" if merged else None,
        "user": {"login": "alice"},
        "head": {"ref": "feature", "sha": f"{number:040x}"},
        "base": {"ref": "main", "sha": BASE},
        "created_at": "2026-09-01T00:00:00Z",
        "updated_at": f"2026-09-02T00:{number % 60:02d}:00Z",
        "html_url": f"https://github.com/acme/repo/pull/{number}",
        "labels": [],
    }


class FakeForge:
    """Answer GitHub API requests for ``acme/repo`` in memory.

    ``head`` is the head commit pull request detail reads return; a test
    changes it to simulate a push. Unknown paths answer 404.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.pulls: list[dict] = [pull(n) for n in range(1, 4)]
        self.head = HEAD_1
        # Heads later detail reads return in turn, each kept as ``head``.
        self.next_heads: list[str] = []
        self.changed_files = 2
        self.files: list[dict] = [
            {
                "filename": "src/app.py",
                "status": "modified",
                "additions": 1,
                "deletions": 1,
                "changes": 2,
                "patch": "@@ -1 +1 @@\n-old\n+new",
            }
        ]
        self.reviews_posted: list[dict] = []
        self.comments_posted: list[dict] = []
        self.overrides: dict[str, httpx.Response] = {}
        self.errors: dict[str, Exception] = {}
        # Seconds each request waits, to let concurrent requests overlap.
        self.delay = 0.0
        self.active = 0
        self.peak = 0

    def count(self, method: str, path: str) -> int:
        return sum(
            1
            for request in self.requests
            if request.method == method and request.url.path == path
        )

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            return self._answer(request)
        finally:
            self.active -= 1

    def _answer(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path in self.errors:
            raise self.errors[path]
        params = request.url.params
        if path in self.overrides:
            return self.overrides[path]
        if path == "/graphql":
            # No GraphQL API, so CI is read over REST.
            return httpx.Response(404, json={"message": "Not Found"})
        if request.method == "GET" and path == "/repos/acme/repo/pulls":
            return self._list(params)
        if path.endswith("/check-runs"):
            return httpx.Response(
                200,
                json={
                    "total_count": 1,
                    "check_runs": [{"status": "completed", "conclusion": "success"}],
                },
            )
        if path.endswith("/status"):
            return httpx.Response(200, json={"total_count": 0, "statuses": []})
        if path == "/repos/acme/repo/actions/runs":
            return httpx.Response(200, json={"workflow_runs": [self._run()]})
        if path == "/repos/acme/repo/issues/7/comments" and request.method == "POST":
            self.comments_posted.append(json.loads(request.content))
            return httpx.Response(201, json={"id": 501})
        match = _PULL.fullmatch(path.removesuffix("/files").removesuffix("/reviews"))
        if match is None:
            return httpx.Response(404, json={"message": "Not Found"})
        number = int(match.group(1))
        if path.endswith("/files"):
            page = int(params.get("page", "1"))
            per_page = int(params.get("per_page", "100"))
            chunk = self.files[(page - 1) * per_page : page * per_page]
            return httpx.Response(200, json=chunk)
        if path.endswith("/reviews"):
            if request.method == "POST":
                self.reviews_posted.append(json.loads(request.content))
                return httpx.Response(200, json={"id": 901})
            return httpx.Response(200, json=[])
        if self.next_heads:
            self.head = self.next_heads.pop(0)
        return httpx.Response(200, json=self._detail(number))

    def _list(self, params: httpx.QueryParams) -> httpx.Response:
        state = params.get("state", "open")
        items = [
            item for item in self.pulls if state == "all" or item["state"] == state
        ]
        if params.get("sort") == "updated":
            items.sort(key=lambda item: item["updated_at"], reverse=True)
        page = int(params.get("page", "1"))
        per_page = int(params.get("per_page", "30"))
        chunk = items[(page - 1) * per_page : page * per_page]
        headers = {}
        if len(items) > page * per_page:
            headers["Link"] = '<https://api.github.com/next>; rel="next"'
        return httpx.Response(200, json=chunk, headers=headers)

    def _detail(self, number: int) -> dict:
        return {
            **pull(number),
            "head": {"ref": "feature", "sha": self.head},
            "base": {"ref": "main", "sha": BASE},
            "body": "Details",
            "changed_files": self.changed_files,
            "requested_reviewers": [],
            "assignees": [],
        }

    def _run(self) -> dict:
        return {
            "id": 77,
            "status": "completed",
            "conclusion": "success",
            "head_branch": "feature",
            "head_sha": self.head,
            "html_url": "https://github.com/acme/repo/actions/runs/77",
            "event": "pull_request",
            "created_at": "2026-09-02T00:00:00Z",
            "updated_at": "2026-09-02T00:05:00Z",
            "run_started_at": "2026-09-02T00:00:00Z",
            "run_duration_ms": 300_000,
        }
