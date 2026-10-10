"""Refuse to publish a release tag that main has not already proved.

A stable ``vX.Y.Z`` tag push runs ``publish.yml`` (PyPI) and
``release-desktop.yml`` (the GitHub Release).  Before either installs, builds
or uploads anything, it runs this program, which passes only when all three
hold for the tagged commit:

1. The commit is on main: the compare ``<sha>...main`` reports ``ahead`` or
   ``identical``.  Any commit main already contains passes, not only its tip.
2. ``docs/releases/<tag>.md`` exists in the tagged tree and is not empty.
3. The newest ``ci.yml`` run for a push to main of exactly this commit, in this
   repository, completed with conclusion ``success``.  A run's conclusion is
   that of its latest attempt, so a partial re-run that turned green passes.

The workflow fetches the two API responses in a separate step that is the
only one holding a token; this program reads them from files, uses the
standard library alone and runs as ``python3 -I``.  It never polls: a check
that fails prints its own line on stderr and the exit status is non-zero.
When main CI is still running, the fix is to re-run the tag workflows once it
is green, never to move or recreate the tag.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

TAG_PATTERN = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
MAIN_BRANCH = "main"
CI_WORKFLOW_PATH = ".github/workflows/ci.yml"
CI_EVENT = "push"
#: Compare statuses of ``<sha>...main`` that mean main contains ``<sha>``.
ON_MAIN_STATUSES = frozenset({"ahead", "identical"})
RETRY_ADVICE = (
    "once the ci.yml push run on main for this commit is green, re-run the "
    "failed jobs of whichever tag workflow failed (publish.yml or "
    "release-desktop.yml); never move or recreate the tag"
)


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _integer(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _check_identity(tag: str, sha: str) -> list[str]:
    failures = []
    if TAG_PATTERN.fullmatch(tag) is None:
        failures.append(f"tag {tag!r} is not a stable vX.Y.Z release tag")
    if SHA_PATTERN.fullmatch(sha) is None:
        failures.append(f"commit {sha!r} is not a full lowercase commit SHA")
    return failures


def _check_ancestry(sha: str, compare: Any) -> list[str]:
    if not isinstance(compare, dict):
        return [f"the compare response for {sha}...{MAIN_BRANCH} is not a JSON object"]
    base = compare.get("base_commit")
    base_sha = _text(base.get("sha")) if isinstance(base, dict) else None
    if base_sha != sha:
        return [
            (
                f"the compare response is for base {base_sha!r}, "
                f"not the tagged commit {sha}"
            )
        ]
    status = compare.get("status")
    ahead_by = compare.get("ahead_by")
    behind_by = compare.get("behind_by")
    if status not in ON_MAIN_STATUSES:
        return [
            (
                f"tagged commit {sha} is not on {MAIN_BRANCH}: compare "
                f"{sha}...{MAIN_BRANCH} reports {status!r} (ahead_by={ahead_by!r}, "
                f"behind_by={behind_by!r}); tag only a commit that {MAIN_BRANCH} "
                "already contains"
            )
        ]
    merge_base = compare.get("merge_base_commit")
    merge_base_sha = (
        _text(merge_base.get("sha")) if isinstance(merge_base, dict) else None
    )
    if _integer(behind_by) != 0 or merge_base_sha != sha:
        return [
            (
                f"the compare response for {sha}...{MAIN_BRANCH} reports {status!r} "
                f"but behind_by={behind_by!r} and merge base {merge_base_sha!r}, "
                "which contradicts it"
            )
        ]
    return []


def _check_notes(tag: str, notes_path: Path) -> list[str]:
    expected = f"docs/releases/{tag}.md"
    if not notes_path.as_posix().endswith(expected):
        return [f"release notes path {str(notes_path)!r} is not {expected}"]
    if not notes_path.is_file():
        return [f"{expected} is missing from the tagged tree"]
    try:
        text = notes_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        return [f"{expected} cannot be read as UTF-8 text: {error}"]
    body = _without_front_matter(text)
    if not body.strip():
        return [f"{expected} is empty in the tagged tree"]
    if body.split("\n", 1)[0] == "---":
        return [f"{expected} starts with '---' after its front matter"]
    return []


def _without_front_matter(text: str) -> str:
    """Drop a leading ``---`` block, as the desktop release's notes step does.

    Lines split on ``\\n`` only, so each one matches awk's ``$0`` exactly.
    """
    lines = text.split("\n")
    if lines[0] != "---":
        return text
    for index, line in enumerate(lines[1:], start=1):
        if line == "---":
            return "\n".join(lines[index + 1 :])
    return ""


def _is_main_ci_push_run(run: Any, sha: str, repository_id: str) -> bool:
    if not isinstance(run, dict):
        return False
    repository = run.get("repository")
    head_repository = run.get("head_repository")
    run_repository_id = (
        _integer(repository.get("id")) if isinstance(repository, dict) else None
    )
    head_repository_id = (
        _integer(head_repository.get("id"))
        if isinstance(head_repository, dict)
        else None
    )
    return (
        run.get("event") == CI_EVENT
        and run.get("head_branch") == MAIN_BRANCH
        and run.get("head_sha") == sha
        and run.get("path") == CI_WORKFLOW_PATH
        and run_repository_id is not None
        and str(run_repository_id) == repository_id
        and head_repository_id == run_repository_id
    )


def _check_main_ci(sha: str, repository_id: str, runs: Any) -> list[str]:
    listed = runs.get("workflow_runs") if isinstance(runs, dict) else None
    if not isinstance(listed, list):
        return ["the ci.yml runs response has no workflow_runs list"]
    matching = [run for run in listed if _is_main_ci_push_run(run, sha, repository_id)]
    if not matching:
        return [
            (
                f"no ci.yml {CI_EVENT} run on {MAIN_BRANCH} exists for {sha} in this "
                "repository (only a commit that was itself pushed to main has one); "
                f"{RETRY_ADVICE}"
            )
        ]
    if any(_integer(run.get("id")) is None for run in matching):
        return ["a ci.yml run for this commit carries no numeric run id"]
    newest = max(matching, key=lambda run: run["id"])
    label = (
        f"the newest ci.yml {CI_EVENT} run on {MAIN_BRANCH} for {sha} "
        f"(run {newest['id']}, attempt {newest.get('run_attempt')!r})"
    )
    status = newest.get("status")
    if status != "completed":
        return [f"{label} is {status!r}, not completed; {RETRY_ADVICE}"]
    conclusion = newest.get("conclusion")
    if conclusion != "success":
        return [f"{label} concluded {conclusion!r}, not success; {RETRY_ADVICE}"]
    return []


def evaluate(
    tag: str,
    sha: str,
    repository_id: int | str,
    compare: Any,
    runs: Any,
    notes_path: Path,
) -> list[str]:
    """Return one message per failed check; an empty list means publish.

    ``compare`` is the parsed ``repos/<repo>/compare/<sha>...main`` response
    and ``runs`` the parsed ``repos/<repo>/actions/workflows/ci.yml/runs``
    response.  Every check runs, so one invocation reports every reason.
    """

    return [
        *_check_identity(tag, sha),
        *_check_ancestry(sha, compare),
        *_check_notes(tag, notes_path),
        *_check_main_ci(sha, str(repository_id).strip(), runs),
    ]


def _load_json(path: Path, label: str, failures: list[str]) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        failures.append(f"cannot read the {label} response {str(path)!r}: {error}")
        return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Refuse to publish a release tag unless its commit is on main, its "
            "release notes exist and main's ci.yml push run on it is green."
        )
    )
    parser.add_argument("--tag", required=True, help="the vX.Y.Z tag being published")
    parser.add_argument("--sha", required=True, help="the tagged commit SHA")
    parser.add_argument(
        "--repository-id", required=True, help="this repository's numeric id"
    )
    parser.add_argument(
        "--compare",
        required=True,
        type=Path,
        help="JSON response of repos/<repo>/compare/<sha>...main",
    )
    parser.add_argument(
        "--runs",
        required=True,
        type=Path,
        help="JSON response of repos/<repo>/actions/workflows/ci.yml/runs",
    )
    parser.add_argument(
        "--notes",
        required=True,
        type=Path,
        help="docs/releases/<tag>.md in the tagged checkout",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    failures: list[str] = []
    compare = _load_json(arguments.compare, "compare", failures)
    runs = _load_json(arguments.runs, "ci.yml runs", failures)
    failures += evaluate(
        arguments.tag,
        arguments.sha,
        arguments.repository_id,
        compare,
        runs,
        arguments.notes,
    )
    if failures:
        for failure in failures:
            print(f"release gate: {failure}", file=sys.stderr)
        print(
            f"release gate: refusing to publish {arguments.tag}; nothing was "
            "built or uploaded",
            file=sys.stderr,
        )
        return 1
    print(
        f"release gate: {arguments.tag} at {arguments.sha} is on {MAIN_BRANCH}, "
        f"has release notes and a green ci.yml {CI_EVENT} run on {MAIN_BRANCH}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
