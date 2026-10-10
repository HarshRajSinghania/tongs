"""Exercise the release tag gate in ``.github/scripts/release_gate.py``.

The fixtures under ``tests/ci/fixtures/release_gate`` are real API responses
for the v1.0.3 tag (commit 790f758), recorded with ``gh api`` from
``repos/andre-motta/tongs/compare/<sha>...main`` and
``repos/andre-motta/tongs/actions/workflows/ci.yml/runs?event=push&branch=main&head_sha=<sha>``.
That run is the partial re-run case as it happened: attempt 1 failed and
attempt 2 succeeded.  Each other file is the recording hand-edited for one
case.  The bulky user objects, the commit list and the file list are trimmed,
and the field names, nesting and value types are left as GitHub returns them.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from tests.ci.verify_desktop_production_gate import ROOT

PROGRAM = ROOT / ".github/scripts/release_gate.py"
FIXTURES = Path(__file__).parent / "fixtures" / "release_gate"
TAG = "v1.0.3"
SHA = "790f75838b9e60a00412d2c5e06ab4531c19b537"
REPOSITORY_ID = "1305350434"


def _load_program() -> ModuleType:
    specification = importlib.util.spec_from_file_location("release_gate", PROGRAM)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


gate = _load_program()


def _fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def notes(tmp_path: Path) -> Path:
    path = tmp_path / "docs" / "releases" / f"{TAG}.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\ntitle: tongs 1.0.3\n---\n\nReleased 2026-09-27.\n")
    return path


def _evaluate(
    notes_path: Path,
    *,
    compare: str = "compare-ahead.json",
    runs: str = "runs-success.json",
    tag: str = TAG,
    sha: str = SHA,
    repository_id: int | str = REPOSITORY_ID,
) -> list[str]:
    return gate.evaluate(
        tag, sha, repository_id, _fixture(compare), _fixture(runs), notes_path
    )


def test_the_recordings_are_the_tagged_commit_on_this_repository() -> None:
    compare = _fixture("compare-ahead.json")
    assert compare["base_commit"]["sha"] == SHA
    assert compare["merge_base_commit"]["sha"] == SHA
    assert compare["status"] == "ahead"
    assert compare["behind_by"] == 0
    (run,) = _fixture("runs-rerun-success.json")["workflow_runs"]
    assert run["head_sha"] == SHA
    assert run["repository"]["id"] == int(REPOSITORY_ID)
    assert run["path"] == gate.CI_WORKFLOW_PATH
    assert (run["event"], run["head_branch"]) == ("push", "main")
    assert (run["run_attempt"], run["status"], run["conclusion"]) == (
        2,
        "completed",
        "success",
    )
    assert run["previous_attempt_url"].endswith(f"/runs/{run['id']}/attempts/1")


@pytest.mark.parametrize("compare", ["compare-ahead.json", "compare-identical.json"])
def test_a_green_tag_on_main_with_notes_passes(notes: Path, compare: str) -> None:
    assert _evaluate(notes, compare=compare) == []


def test_a_numeric_repository_id_passes_like_its_string_form(notes: Path) -> None:
    assert _evaluate(notes, repository_id=int(REPOSITORY_ID)) == []


@pytest.mark.parametrize(
    ("compare", "status"),
    [("compare-behind.json", "'behind'"), ("compare-diverged.json", "'diverged'")],
)
def test_a_commit_main_does_not_contain_fails(
    notes: Path, compare: str, status: str
) -> None:
    (failure,) = _evaluate(notes, compare=compare)
    assert failure.startswith(f"tagged commit {SHA} is not on main")
    assert status in failure


def test_an_ancestry_status_contradicted_by_its_counts_fails(notes: Path) -> None:
    compare = _fixture("compare-ahead.json")
    compare["behind_by"] = 1
    failures = gate.evaluate(
        TAG, SHA, REPOSITORY_ID, compare, _fixture("runs-success.json"), notes
    )
    assert len(failures) == 1
    assert "contradicts" in failures[0]
    compare = _fixture("compare-ahead.json")
    compare["merge_base_commit"] = _fixture("compare-diverged.json")[
        "merge_base_commit"
    ]
    failures = gate.evaluate(
        TAG, SHA, REPOSITORY_ID, compare, _fixture("runs-success.json"), notes
    )
    assert len(failures) == 1
    assert "contradicts" in failures[0]


def test_a_compare_for_another_base_fails(notes: Path) -> None:
    other = "f4506f16310000000000000000000000000000ff"
    failures = gate.evaluate(
        TAG,
        other,
        REPOSITORY_ID,
        _fixture("compare-ahead.json"),
        _fixture("runs-success.json"),
        notes,
    )
    assert f"not the tagged commit {other}" in failures[0]


def test_missing_release_notes_fail(tmp_path: Path) -> None:
    missing = tmp_path / "docs" / "releases" / f"{TAG}.md"
    assert _evaluate(missing) == [
        f"docs/releases/{TAG}.md is missing from the tagged tree"
    ]


@pytest.mark.parametrize(
    "content",
    [
        "",
        "\n",
        "  \n\t\n",
        "---\ntitle: v1.0.4\n---\n",
        "---\ntitle: v1.0.4\n---\n\n  \n",
    ],
)
def test_empty_release_notes_fail(notes: Path, content: str) -> None:
    notes.write_text(content)
    assert _evaluate(notes) == [f"docs/releases/{TAG}.md is empty in the tagged tree"]


def test_release_notes_with_front_matter_and_a_body_pass(notes: Path) -> None:
    notes.write_text("---\ntitle: v1.0.4\n---\n\nFixes stuck review drafts.\n")
    assert _evaluate(notes) == []


@pytest.mark.parametrize(
    "content", ["---\ntitle: v1.0.4\n---\n---\nBody\n", "---\ntitle\n---\n---\n"]
)
def test_notes_whose_body_opens_with_a_delimiter_fail(
    notes: Path, content: str
) -> None:
    notes.write_text(content)
    (failure,) = _evaluate(notes)
    assert failure.startswith(f"docs/releases/{TAG}.md ")


@pytest.mark.parametrize(
    "head_repository", [None, "andre-motta/tongs", {}, {"id": "1305350434"}]
)
def test_a_run_without_a_usable_head_repository_is_ignored(
    head_repository: Any,
) -> None:
    (run,) = _fixture("runs-success.json")["workflow_runs"]
    assert not gate._is_main_ci_push_run(
        {**run, "head_repository": head_repository}, SHA, REPOSITORY_ID
    )
    missing = {key: value for key, value in run.items() if key != "head_repository"}
    assert not gate._is_main_ci_push_run(missing, SHA, REPOSITORY_ID)


def test_a_green_main_run_from_a_fork_head_is_ignored() -> None:
    (run,) = _fixture("runs-success.json")["workflow_runs"]
    forked = {**run, "head_repository": {**run["head_repository"], "id": 1399999999}}
    assert not gate._is_main_ci_push_run(forked, SHA, REPOSITORY_ID)
    assert gate._is_main_ci_push_run(run, SHA, REPOSITORY_ID)


def test_release_notes_for_another_tag_fail(notes: Path) -> None:
    other = notes.with_name("v1.0.2.md")
    other.write_text("Released.\n")
    (failure,) = _evaluate(other)
    assert failure.endswith(f"is not docs/releases/{TAG}.md")


def test_no_main_push_run_for_the_commit_fails(notes: Path) -> None:
    (failure,) = _evaluate(notes, runs="runs-empty.json")
    assert failure.startswith(f"no ci.yml push run on main exists for {SHA}")
    assert "never move or recreate the tag" in failure


@pytest.mark.parametrize(
    ("runs", "status"),
    [("runs-in-progress.json", "'in_progress'"), ("runs-queued.json", "'queued'")],
)
def test_a_main_run_still_running_fails_without_polling(
    notes: Path, runs: str, status: str
) -> None:
    (failure,) = _evaluate(notes, runs=runs)
    assert f"is {status}, not completed" in failure
    assert "re-run the failed jobs of whichever tag workflow failed" in failure
    assert "never move or recreate the tag" in failure


def test_a_failed_main_run_fails(notes: Path) -> None:
    (failure,) = _evaluate(notes, runs="runs-failed.json")
    assert "concluded 'failure', not success" in failure
    assert "(run 36336399567, attempt 1)" in failure


def test_the_latest_attempt_of_a_partial_re_run_decides(notes: Path) -> None:
    # Attempt 1 of this real run failed; the run reports attempt 2's result.
    assert _evaluate(notes, runs="runs-rerun-success.json") == []
    (failure,) = _evaluate(notes, runs="runs-rerun-in-progress.json")
    assert "(run 36336399567, attempt 2) is 'in_progress'" in failure


def test_the_newest_matching_run_decides(notes: Path) -> None:
    runs = _fixture("runs-newer-failed.json")["workflow_runs"]
    assert [run["conclusion"] for run in runs] == ["failure", "success"]
    (failure,) = _evaluate(notes, runs="runs-newer-failed.json")
    assert "(run 36399999999, attempt 1) concluded 'failure'" in failure


@pytest.mark.parametrize(
    "runs",
    [
        "runs-other-branch.json",
        "runs-other-event.json",
        "runs-other-path.json",
        "runs-other-repository.json",
        "runs-other-sha.json",
    ],
)
def test_a_green_run_that_is_not_main_ci_for_this_commit_is_ignored(
    notes: Path, runs: str
) -> None:
    (run,) = _fixture(runs)["workflow_runs"]
    assert (run["status"], run["conclusion"]) == ("completed", "success")
    (failure,) = _evaluate(notes, runs=runs)
    assert failure.startswith(f"no ci.yml push run on main exists for {SHA}")


def test_newer_foreign_runs_do_not_displace_the_green_main_run(notes: Path) -> None:
    runs = _fixture("runs-success-with-foreign-runs.json")["workflow_runs"]
    assert max(run["id"] for run in runs) > runs[-1]["id"]
    assert {run["conclusion"] for run in runs[:-1]} == {"failure", "cancelled", None}
    assert _evaluate(notes, runs="runs-success-with-foreign-runs.json") == []


def test_a_workflow_path_merely_ending_in_ci_yml_is_not_ci() -> None:
    (run,) = _fixture("runs-other-path.json")["workflow_runs"]
    assert run["path"].endswith("ci.yml")
    assert not gate._is_main_ci_push_run(run, SHA, REPOSITORY_ID)


@pytest.mark.parametrize(
    ("tag", "message"),
    [
        ("v1.0.3rc1", "is not a stable vX.Y.Z release tag"),
        ("1.0.3", "is not a stable vX.Y.Z release tag"),
        ("v01.0.3", "is not a stable vX.Y.Z release tag"),
    ],
)
def test_a_non_release_tag_fails(tmp_path: Path, tag: str, message: str) -> None:
    path = tmp_path / "docs" / "releases" / f"{tag}.md"
    path.parent.mkdir(parents=True)
    path.write_text("Released.\n")
    failures = _evaluate(path, tag=tag)
    assert failures == [f"tag {tag!r} {message}"]


@pytest.mark.parametrize(
    ("compare", "runs"),
    [(None, None), ([], {}), ({"status": "ahead"}, {"workflow_runs": None})],
)
def test_malformed_responses_fail_closed(notes: Path, compare: Any, runs: Any) -> None:
    failures = gate.evaluate(TAG, SHA, REPOSITORY_ID, compare, runs, notes)
    assert len(failures) == 2
    assert "compare response" in failures[0]
    assert "runs response" in failures[1]


def test_every_failure_is_reported_at_once(tmp_path: Path) -> None:
    failures = _evaluate(
        tmp_path / "docs" / "releases" / f"{TAG}.md",
        compare="compare-diverged.json",
        runs="runs-in-progress.json",
    )
    assert len(failures) == 3
    assert "is not on main" in failures[0]
    assert "is missing" in failures[1]
    assert "not completed" in failures[2]


def _run_program(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", str(PROGRAM), *arguments],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def _arguments(notes_path: Path, compare: str, runs: str) -> list[str]:
    return [
        "--tag",
        TAG,
        "--sha",
        SHA,
        "--repository-id",
        REPOSITORY_ID,
        "--compare",
        str(FIXTURES / compare),
        "--runs",
        str(FIXTURES / runs),
        "--notes",
        str(notes_path),
    ]


def test_the_program_exits_zero_only_when_every_check_passes(notes: Path) -> None:
    passed = _run_program(*_arguments(notes, "compare-ahead.json", "runs-success.json"))
    assert passed.returncode == 0, passed.stderr
    assert passed.stderr == ""
    assert "v1.0.3 at 790f758" in passed.stdout

    notes.unlink()
    failed = _run_program(
        *_arguments(notes, "compare-behind.json", "runs-in-progress.json")
    )
    assert failed.returncode == 1
    lines = failed.stderr.splitlines()
    assert len(lines) == 4
    assert all(line.startswith("release gate: ") for line in lines)
    assert "is not on main" in lines[0]
    assert "is missing" in lines[1]
    assert "not completed" in lines[2]
    assert "refusing to publish v1.0.3" in lines[3]


def test_the_program_fails_on_an_unreadable_response(
    notes: Path, tmp_path: Path
) -> None:
    broken = tmp_path / "compare.json"
    broken.write_text("{not json")
    arguments = _arguments(notes, "compare-ahead.json", "runs-success.json")
    arguments[arguments.index("--compare") + 1] = str(broken)
    completed = _run_program(*arguments)
    assert completed.returncode == 1
    assert "cannot read the compare response" in completed.stderr
    assert "refusing to publish" in completed.stderr


def test_the_program_uses_only_the_standard_library() -> None:
    # The gate runs as ``python3 -I`` on the runner before anything is
    # installed, so importing it must not need any third-party module.
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            (
                "import sys, runpy\n"
                "before = set(sys.modules)\n"
                f"runpy.run_path({str(PROGRAM)!r})\n"
                "extra = {name.split('.')[0] for name in set(sys.modules) - before}\n"
                "foreign = sorted(name for name in extra\n"
                "    if name not in sys.stdlib_module_names and not name.startswith('_'))\n"
                "print(foreign)\n"
            ),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    assert completed.stdout.strip() == "[]"
