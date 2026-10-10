"""Pin the shape of the desktop release publication path in release-desktop.yml.

The installer trusts exactly one workflow path at exactly one ref shape:
``.github/workflows/release-desktop.yml`` at ``refs/tags/vX.Y.Z`` from a
``push`` trigger.  These cases hold the workflow to that contract from the
other side: the tag filter admits only stable tags, the signing job runs on
those tags, the publish job is the only job holding ``contents: write`` and
runs only for a tag push, every step that can create anything is preceded by
the installer-path verification, and a dry run can never reach publication.
Both tag workflows run the release gate (``.github/scripts/release_gate.py``)
right after checkout, before any install, build or upload, and only its fetch
step sees the token; a manual dispatch from a ref other than main fails.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.ci.verify_desktop_production_gate import ROOT
from tongs.desktop.installer.metadata import OFFICIAL_WORKFLOW_PATH

WORKFLOW = ROOT / OFFICIAL_WORKFLOW_PATH
PUBLISH_WORKFLOW = ROOT / ".github/workflows/publish.yml"
RELEASE_TAG_FILTER = "v[0-9]+.[0-9]+.[0-9]+"
PUBLICATION_PROGRAM = "tests/integration/desktop/release_publication.py"
PAYLOAD_CONTRACT_PROGRAM = "tests/integration/desktop/rpm_payload_contract.py"
GATE_PROGRAM = ".github/scripts/release_gate.py"
TAG_PUSH = "github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')"
#: The first job of each tag workflow; every publishing job depends on it.
GATE_JOBS = (("publish.yml", "build"), ("release-desktop.yml", "candidate-archive"))
GATE_FETCHES = [
    (
        'gh api "repos/$GITHUB_REPOSITORY/compare/$GITHUB_SHA...main" '
        '> "$RUNNER_TEMP/release-gate-compare.json"'
    ),
    (
        'gh api "repos/$GITHUB_REPOSITORY/actions/workflows/ci.yml/runs'
        '?event=push&branch=main&head_sha=$GITHUB_SHA&per_page=100" '
        '> "$RUNNER_TEMP/release-gate-runs.json"'
    ),
]
#: Steps that install, build, sign or upload; none may run before the gate.
_GATED_WORK = re.compile(
    r"pip3?\s+install|-m\s+build|run_hosted\.sh|candidate_attestation\.py"
    r"|git\s+archive|-m\s+venv|gh\s+release"
)


@pytest.fixture(scope="module")
def workflow() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW.read_text())


@pytest.fixture(scope="module")
def jobs(workflow: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return workflow["jobs"]


def _run_lines(job: dict[str, Any]) -> list[str]:
    return [" ".join((step.get("run") or "").split()) for step in job["steps"]]


def _step_index(job: dict[str, Any], marker: str) -> int:
    for index, line in enumerate(_run_lines(job)):
        if marker in line:
            return index
    raise AssertionError(f"no step runs {marker!r}")


def test_the_tag_filter_admits_only_stable_version_tags(
    workflow: dict[str, Any],
) -> None:
    triggers = workflow[True]
    assert triggers["push"]["tags"] == [RELEASE_TAG_FILTER]
    # Only a release tag push runs this workflow; branch pushes do not.
    assert set(triggers["push"]) == {"tags"}
    # The same filter pattern must gate the PyPI publication, or one tag would
    # publish the core without the desktop or the other way round.
    publish = yaml.safe_load(PUBLISH_WORKFLOW.read_text())
    assert RELEASE_TAG_FILTER in publish[True]["push"]["tags"] or publish[True]["push"][
        "tags"
    ] == ["v*"], publish[True]["push"]["tags"]
    dispatch = triggers["workflow_dispatch"]["inputs"]["dry_run"]
    assert dispatch["type"] == "boolean"
    assert dispatch["default"] is True


def test_pull_requests_into_main_run_only_the_no_signing_validation(
    workflow: dict[str, Any],
    jobs: dict[str, dict[str, Any]],
) -> None:
    triggers = workflow[True]
    assert triggers["pull_request"]["branches"] == ["main"]
    # The paths filter stays: this workflow is not the required check.
    assert triggers["pull_request"]["paths"]
    # Only the unconditional validation job can run for a pull request.  Every
    # other job is gated on a push or a dry-run dispatch and never names the
    # pull_request event.
    assert "if" not in jobs["no-signing-validation"]
    assert "needs" not in jobs["no-signing-validation"]
    for name, job in jobs.items():
        if name == "no-signing-validation":
            continue
        condition = " ".join(job["if"].split())
        assert "pull_request" not in condition, name
        events = set(re.findall(r"github\.event_name == '([a-z_]+)'", condition))
        assert events and events <= {"push", "workflow_dispatch"}, (name, events)
    assert jobs["no-signing-validation"]["permissions"] == {"contents": "read"}


def test_the_workflow_level_permissions_are_read_only(
    workflow: dict[str, Any],
) -> None:
    assert workflow["permissions"] == {"contents": "read"}


def test_only_the_publish_job_can_write_contents(
    jobs: dict[str, dict[str, Any]],
) -> None:
    writers = {
        name
        for name, job in jobs.items()
        if (job.get("permissions") or {}).get("contents") == "write"
    }
    assert writers == {"release-publish"}
    assert jobs["release-publish"]["permissions"] == {"contents": "write"}
    # The unprivileged archive build must not mint or attest anything.  It
    # reads main's ci.yml runs for the release gate and nothing more.
    assert jobs["candidate-archive"]["permissions"] == {
        "contents": "read",
        "actions": "read",
    }
    # The signing job mints the OIDC token; it must not also be able to write.
    assert jobs["candidate-attestation"]["permissions"] == {
        "contents": "read",
        "id-token": "write",
        "attestations": "write",
    }


def test_the_signing_and_publish_jobs_run_for_a_tag_push_only_as_expected(
    jobs: dict[str, dict[str, Any]],
) -> None:
    for name in ("candidate-archive", "candidate-attestation"):
        condition = " ".join(jobs[name]["if"].split())
        assert "startsWith(github.ref, 'refs/tags/v')" in condition
        assert "github.event_name == 'push'" in condition
        assert "inputs.dry_run == true" in condition
        # A dry run is dispatched from main; no other branch builds a candidate.
        assert re.findall(r"refs/heads/[\w./-]+", condition) == ["refs/heads/main"]
    publish = " ".join(jobs["release-publish"]["if"].split())
    assert "github.event_name == 'push'" in publish
    assert "startsWith(github.ref, 'refs/tags/v')" in publish
    assert "workflow_dispatch" not in publish
    assert "dry_run" not in publish
    assert "github.repository == 'andre-motta/tongs'" in publish
    assert "github.repository_id == '1305350434'" in publish
    assert "github.repository_owner_id == '30708955'" in publish
    for upstream in ("candidate-archive", "candidate-attestation", "release-rpm"):
        assert f"needs.{upstream}.result == 'success'" in publish
    assert set(jobs["release-publish"]["needs"]) == {
        "candidate-archive",
        "candidate-attestation",
        "release-rpm",
    }


def test_a_dry_run_rebuilds_rpms_but_never_publishes(
    jobs: dict[str, dict[str, Any]],
) -> None:
    rpm = " ".join(jobs["release-rpm"]["if"].split())
    assert "github.event_name == 'workflow_dispatch' && inputs.dry_run == true" in rpm
    assert "github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')" in rpm
    assert jobs["release-rpm"]["permissions"] == {
        "contents": "read",
        "actions": "read",
    }


def test_the_release_version_is_derived_from_the_ref_and_exported(
    jobs: dict[str, dict[str, Any]],
) -> None:
    archive = jobs["candidate-archive"]
    assert archive["outputs"]["release-version"] == (
        "${{ steps.release.outputs.release_version }}"
    )
    assert archive["outputs"]["release-tag"] == "${{ steps.release.outputs.tag }}"
    assert archive["outputs"]["archive-name"] == (
        "${{ steps.release.outputs.archive_name }}"
    )
    derive = next(step for step in archive["steps"] if step.get("id") == "release")
    assert "refs/tags/v*)" in derive["run"]
    assert re.search(r"release_version=\$\{tag#v\}", derive["run"])
    build = next(
        step for step in archive["steps"] if "run_hosted.sh" in (step.get("run") or "")
    )
    assert build["env"]["TONGS_RELEASE_VERSION"] == (
        "${{ steps.release.outputs.release_version }}"
    )
    # The attested archive subject is the derived name, never a literal.
    attest = next(
        step
        for step in jobs["candidate-attestation"]["steps"]
        if step.get("id") == "attest"
    )
    assert (
        "${{ needs.candidate-archive.outputs.archive-name }}"
        in (attest["with"]["subject-path"])
    )
    assert "0.5.0" not in attest["with"]["subject-path"]


def test_the_rpm_job_binds_the_contract_to_the_release_version(
    jobs: dict[str, dict[str, Any]],
) -> None:
    lines = _run_lines(jobs["release-rpm"])
    contract = next(line for line in lines if PAYLOAD_CONTRACT_PROGRAM in line)
    assert '--release-version "$RELEASE_VERSION"' in contract
    assert '--source-commit "$GITHUB_SHA"' in contract
    assert '--artifact-id "${{ needs.candidate-attestation.outputs.artifact-id }}"' in (
        contract
    )
    producer = next(
        line for line in lines if "packaging/rpm/desktop/run_hosted.sh" in line
    )
    assert '--accepted-dir "$RUNNER_TEMP/tongs-desktop-candidate/archive"' in producer
    assert '--payload-manifest "$RUNNER_TEMP/payload-input-contract.json"' in producer
    checkout = jobs["release-rpm"]["steps"][0]
    assert checkout["with"]["fetch-depth"] == 0, "git describe needs the tag"
    assert checkout["with"]["persist-credentials"] is False


def test_verification_precedes_every_creating_step(
    jobs: dict[str, dict[str, Any]],
) -> None:
    job = jobs["release-publish"]
    assemble = _step_index(job, f"{PUBLICATION_PROGRAM} assemble")
    verify = _step_index(job, f"{PUBLICATION_PROGRAM} verify --tag")
    absent = _step_index(job, f"{PUBLICATION_PROGRAM} require-absent")
    create = _step_index(job, "gh release create")
    draft_check = _step_index(job, "--expect-draft")
    publish = _step_index(job, "gh release edit")
    final_check = _step_index(
        job,
        f'{PUBLICATION_PROGRAM} verify-published --tag "$RELEASE_TAG" --assets-dir "$RELEASE_ASSETS" --report',
    )
    assert assemble < verify < absent < create < draft_check < publish < final_check
    lines = _run_lines(job)
    assert lines[create].count("gh release create") == 1
    assert "--draft" in lines[create]
    assert "--verify-tag" in lines[create]
    assert '--notes-file "$RELEASE_NOTES"' in lines[create]
    assert '--notes "$RELEASE_NOTES"' in lines[verify]
    assert "--draft=false" in lines[publish]
    assert "--latest" in lines[publish]
    assert sum("gh release" in line for line in lines) == 2
    # Nothing in the workflow deletes or edits a release outside these steps.
    text = WORKFLOW.read_text()
    assert "gh release delete" not in text
    assert text.count("gh release edit") == 1


def test_the_publish_job_checks_out_without_credentials_and_binds_the_tag(
    jobs: dict[str, dict[str, Any]],
) -> None:
    job = jobs["release-publish"]
    checkout = job["steps"][0]
    assert checkout["with"] == {
        "ref": "${{ github.sha }}",
        "fetch-depth": 1,
        "persist-credentials": False,
    }
    guard = _run_lines(job)[_step_index(job, 'test -s "docs/releases/$RELEASE_TAG.md"')]
    assert 'test "$GITHUB_REF" = "refs/tags/$RELEASE_TAG"' in guard
    assert 'test "$(git rev-parse HEAD)" = "$GITHUB_SHA"' in guard
    assert job["env"]["RELEASE_TAG"] == (
        "${{ needs.candidate-archive.outputs.release-tag }}"
    )


def test_every_release_upload_is_retry_safe_and_retained_for_fourteen_days(
    jobs: dict[str, dict[str, Any]],
) -> None:
    uploads = 0
    for name in ("candidate-attestation", "release-rpm", "release-publish"):
        for step in jobs[name]["steps"]:
            if "upload-artifact" not in str(step.get("uses", "")):
                continue
            uploads += 1
            with_block = step["with"]
            artifact = with_block["name"]
            if artifact.startswith("${{ steps.names.outputs.artifact_name }}"):
                names = next(
                    step for step in jobs[name]["steps"] if step.get("id") == "names"
                )
                artifact = names["run"]
            assert "GITHUB_RUN_ID" in artifact or "github.run_id" in artifact
            assert "GITHUB_RUN_ATTEMPT" in artifact or "github.run_attempt" in artifact
            assert with_block["retention-days"] == 14
            assert with_block["if-no-files-found"] == "error"
    assert uploads == 3


def test_the_release_body_drops_the_site_front_matter(
    jobs: dict[str, dict[str, Any]], tmp_path: Path
) -> None:
    # docs/releases/<tag>.md is also a site page with Starlight front matter.
    # Run the workflow's own awk program on the shipped notes and require a
    # body that starts at the prose.
    job = jobs["release-publish"]
    index = _step_index(job, 'test -s "docs/releases/$RELEASE_TAG.md"')
    require = job["steps"][index]["run"]
    match = re.search(r"awk '([^']+)'", require)
    assert match is not None
    notes = ROOT / "docs/releases/v1.0.0.md"
    assert notes.read_text().startswith("---\n")
    body = subprocess.run(
        ["awk", match.group(1), str(notes)], check=True, capture_output=True, text=True
    ).stdout
    assert body.strip()
    assert not body.startswith("---")
    assert "slug:" not in body.split("\n\n", 1)[0]
    assert '> "$RELEASE_NOTES"' in require


def _job_script(job: dict[str, Any]) -> str:
    return "\n".join(step.get("run") or "" for step in job.get("steps", []))


def test_every_candidate_interpreter_is_isolated_from_the_runner(
    jobs: dict[str, dict[str, Any]],
) -> None:
    """Each job that builds the candidate venv must refuse ambient Python.

    The signing and publish jobs hold tokens while this interpreter runs, so a
    user site-packages directory or a tongs imported from outside the checked
    out workspace would run foreign code with those tokens.
    """

    isolated = 0
    for name, job in jobs.items():
        script = _job_script(job)
        if "tongs-candidate-venv" not in script:
            continue
        isolated += 1
        assert "printf 'PYTHONNOUSERSITE=1\\n' >> \"$GITHUB_ENV\"" in script, name
        assert "assert site.ENABLE_USER_SITE is False" in script, name
        assert "assert tongs_path.is_relative_to(workspace)" in script, name
        calls = re.findall(r'"\$CANDIDATE_PYTHON"\s+(\S+)', script)
        assert calls and set(calls) == {"-I"}, name
        # The release programs run only through the isolated candidate venv.
        for program in re.findall(
            r"(\S+)\s+-I\s+tests/integration/desktop/"
            r"(?:candidate_attestation|release_publication)\.py|"
            r"(?<![\w/])(tests/integration/desktop/"
            r"(?:candidate_attestation|release_publication)\.py)",
            script,
        ):
            assert program[0] == '"$CANDIDATE_PYTHON"', (name, program)
        installs = re.findall(r'"\$CANDIDATE_PYTHON" -I -m pip install([^\n]*)', script)
        assert installs, name
        for arguments in installs:
            assert "--isolated" in arguments.split(), name
    # Validation, transfer, signing and publish.
    assert isolated == 4


def test_attestations_stay_off_the_package_registry(
    jobs: dict[str, dict[str, Any]],
) -> None:
    attest_steps = [
        step
        for job in jobs.values()
        for step in job.get("steps", [])
        if str(step.get("uses", "")).startswith("actions/attest@")
    ]
    assert attest_steps
    for step in attest_steps:
        assert step["with"]["push-to-registry"] is False, step.get("name")
        assert step["with"]["create-storage-record"] is False, step.get("name")


def _gate_job(name: str, job_id: str) -> dict[str, Any]:
    document = yaml.safe_load((ROOT / ".github/workflows" / name).read_text())
    return document["jobs"][job_id]


def _shell_commands(run: str) -> list[str]:
    joined = run.replace("\\\n", " ")
    return [" ".join(line.split()) for line in joined.splitlines() if line.strip()]


def _load_gate() -> Any:
    specification = importlib.util.spec_from_file_location(
        "release_gate_for_workflow_test", ROOT / GATE_PROGRAM
    )
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


@pytest.mark.parametrize(("name", "job_id"), GATE_JOBS)
def test_the_release_gate_runs_right_after_checkout_before_any_work(
    name: str, job_id: str
) -> None:
    job = _gate_job(name, job_id)
    steps = job["steps"]
    assert str(steps[0]["uses"]).startswith("actions/checkout@")
    fetch, check = steps[1], steps[2]
    assert [
        line for line in _shell_commands(fetch["run"]) if line.startswith("gh ")
    ] == GATE_FETCHES
    commands = _shell_commands(check["run"])
    assert 'test "$(git rev-parse HEAD)" = "$GITHUB_SHA"' in commands
    (gate,) = [line for line in commands if GATE_PROGRAM in line]
    assert gate.startswith(f"python3 -I {GATE_PROGRAM} ")
    # Every later step that installs, builds, signs or uploads runs after it.
    later_work = [
        index
        for index, step in enumerate(steps)
        if _GATED_WORK.search(" ".join((step.get("run") or "").split()))
        or str(step.get("uses", "")).startswith(
            ("actions/setup-python@", "actions/upload-artifact@")
        )
    ]
    assert later_work and min(later_work) > 2, later_work
    # The job holds read scopes only, actions: read for the runs list.
    assert job["permissions"] == {"contents": "read", "actions": "read"}
    if name == "publish.yml":
        # This workflow runs on a release tag push alone, so the gate always runs.
        assert "if" not in fetch and "if" not in check
    else:
        assert " ".join(fetch["if"].split()) == TAG_PUSH
        assert " ".join(check["if"].split()) == TAG_PUSH


@pytest.mark.parametrize(("name", "job_id"), GATE_JOBS)
def test_only_the_gate_fetch_step_sees_the_token(name: str, job_id: str) -> None:
    document = yaml.safe_load((ROOT / ".github/workflows" / name).read_text())
    job = document["jobs"][job_id]
    assert "GH_TOKEN" not in (document.get("env") or {})
    assert "GH_TOKEN" not in (job.get("env") or {})
    with_token = [
        index
        for index, step in enumerate(job["steps"])
        if "env" in step and "GH_TOKEN" in step["env"]
    ]
    assert with_token == [1]
    assert job["steps"][1]["env"] == {"GH_TOKEN": "${{ github.token }}"}
    callers = [
        index
        for index, step in enumerate(job["steps"])
        if re.search(r"(^|[\s;&|(])gh\s", step.get("run") or "")
    ]
    assert callers == [1]
    # The gate program itself reads files and holds no credential.
    assert "env" not in job["steps"][2]


@pytest.mark.parametrize(("name", "job_id"), GATE_JOBS)
def test_the_workflow_passes_the_gate_exactly_its_arguments(
    name: str, job_id: str
) -> None:
    check = _gate_job(name, job_id)["steps"][2]
    (gate,) = [line for line in _shell_commands(check["run"]) if GATE_PROGRAM in line]
    tokens = shlex.split(gate)
    assert tokens[:3] == ["python3", "-I", GATE_PROGRAM]
    parser = _load_gate().build_parser()
    arguments = vars(parser.parse_args(tokens[3:]))
    assert arguments["tag"] == "$GITHUB_REF_NAME"
    assert str(arguments["notes"]) == "docs/releases/$GITHUB_REF_NAME.md"
    assert arguments["sha"] == "$GITHUB_SHA"
    assert arguments["repository_id"] == "$GITHUB_REPOSITORY_ID"
    assert str(arguments["compare"]) == "$RUNNER_TEMP/release-gate-compare.json"
    assert str(arguments["runs"]) == "$RUNNER_TEMP/release-gate-runs.json"
    flags = {token for token in tokens if token.startswith("--")}
    accepted = {
        option
        for action in parser._actions
        for option in action.option_strings
        if option.startswith("--") and option != "--help"
    }
    assert flags == accepted


def test_every_publishing_job_depends_on_the_gate_job(
    jobs: dict[str, dict[str, Any]],
) -> None:
    for name in ("candidate-attestation", "release-rpm", "release-publish"):
        assert "candidate-archive" in jobs[name]["needs"], name
        condition = " ".join(jobs[name]["if"].split())
        assert "needs.candidate-archive.result == 'success'" in condition, name
        assert "always()" not in condition, name
    publish = yaml.safe_load(PUBLISH_WORKFLOW.read_text())
    assert set(publish[True]) == {"push"}
    assert set(publish[True]["push"]) == {"tags"}
    assert publish["jobs"]["publish"]["needs"] == "build"
    # No condition, so the upload runs only when the gated build succeeded.
    assert "if" not in publish["jobs"]["publish"]


def _dispatch_guard(jobs: dict[str, dict[str, Any]]) -> str:
    step = jobs["no-signing-validation"]["steps"][0]
    assert "if" not in step
    assert "env" not in step
    return step["run"]


@pytest.mark.parametrize(
    ("event", "ref", "allowed"),
    [
        ("workflow_dispatch", "refs/heads/main", True),
        ("workflow_dispatch", "refs/heads/claude/patch-train-R1", False),
        ("workflow_dispatch", "refs/heads/main-backport", False),
        ("workflow_dispatch", "refs/tags/v1.0.4", False),
        ("push", "refs/tags/v1.0.4", True),
        ("pull_request", "refs/pull/388/merge", True),
    ],
)
def test_a_manual_dispatch_from_any_ref_but_main_fails(
    jobs: dict[str, dict[str, Any]], event: str, ref: str, allowed: bool
) -> None:
    # The guard is the first step of the one job that runs for every event, so
    # a dispatch from a branch fails instead of skipping every candidate job.
    script = _dispatch_guard(jobs)
    completed = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "GITHUB_EVENT_NAME": event,
            "GITHUB_REF": ref,
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert (completed.returncode == 0) is allowed, completed.stderr
    if not allowed:
        assert f"not {ref}" in completed.stderr
