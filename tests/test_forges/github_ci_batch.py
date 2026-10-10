"""Answer the batched GitHub CI GraphQL query from fixed data in tests.

``GitHubClient`` coalesces per-pull-request CI reads into one GraphQL query
with an alias per repository (``r0``) and per pull request (``p12``). These
helpers read that query back and build the response a GitHub server sends.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable

import httpx

_REPOSITORY = re.compile(
    r"(r\d+): repository\(owner: \$(o\d+), name: \$(n\d+)\) \{"
    r"((?: p\d+: pullRequest\(number: \d+\) \{ \.\.\.ci \})+) \}"
)
_PULL = re.compile(r"(p\d+): pullRequest\(number: (\d+)\)")

# The pull request alias of a batch answer: owner, repository and number.
PullKey = tuple[str, str, int]


def is_ci_batch(request: httpx.Request) -> bool:
    """Return whether a request is the batched CI query."""
    if request.method != "POST" or not request.url.path.endswith("/graphql"):
        return False
    return "fragment ci on PullRequest" in json.loads(request.content)["query"]


def batch_pulls(request: httpx.Request) -> list[PullKey]:
    """Return the pull requests one batched CI query asks for, in order."""
    body = json.loads(request.content)
    variables = body.get("variables", {})
    pulls: list[PullKey] = []
    for match in _REPOSITORY.finditer(body["query"]):
        owner = variables[match.group(2)]
        repo = variables[match.group(3)]
        for pull in _PULL.finditer(match.group(4)):
            pulls.append((owner, repo, int(pull.group(2))))
    return pulls


def answer_ci_batch(
    request: httpx.Request,
    pull: Callable[[str, str, int], dict | None],
    *,
    errors: list[dict] | None = None,
) -> httpx.Response:
    """Answer a batched CI query, asking ``pull`` for each pull request node."""
    body = json.loads(request.content)
    variables = body.get("variables", {})
    data: dict[str, dict] = {}
    for match in _REPOSITORY.finditer(body["query"]):
        owner = variables[match.group(2)]
        repo = variables[match.group(3)]
        data[match.group(1)] = {
            alias: pull(owner, repo, int(number))
            for alias, number in _PULL.findall(match.group(4))
        }
    document: dict[str, object] = {"data": data}
    if errors:
        document["errors"] = errors
    return httpx.Response(200, json=document)


def check_run(conclusion: str | None, status: str = "COMPLETED") -> dict:
    """Return one rollup check run node, in GraphQL's upper case enums."""
    return {
        "__typename": "CheckRun",
        "status": status.upper(),
        "conclusion": conclusion.upper() if conclusion else None,
    }


def status_context(state: str) -> dict:
    """Return one rollup commit status node."""
    return {"__typename": "StatusContext", "state": state.upper()}


def pull_node(
    head: str,
    checks: list[dict] | None = None,
    *,
    total: int | None = None,
    commit: str | None = None,
) -> dict:
    """Return a batched pull request node for its head commit and checks.

    ``checks`` None means the commit has no rollup at all. ``total`` reports
    more checks than the nodes listed, and ``commit`` a last commit other
    than the head.
    """
    rollup = (
        None
        if checks is None
        else {
            "contexts": {
                "totalCount": len(checks) if total is None else total,
                "nodes": checks,
            }
        }
    )
    return {
        "headRefOid": head,
        "commits": {
            "nodes": [{"commit": {"oid": commit or head, "statusCheckRollup": rollup}}]
        },
    }
