"""Hold every CI file to one credential, permission and timeout contract.

Each workflow under ``.github/workflows`` is loaded and checked for rules
that apply to every file, not only to the release workflows:

* every file declares ``permissions`` explicitly, at the top level or on
  every job, so no job silently inherits the repository default token scope;
* every ``actions/checkout`` step sets ``persist-credentials: false``, so no
  later step (a build backend, an npm install script) can read the job token
  from ``.git/config``;
* every job that runs steps sets ``timeout-minutes``, so a hung job cannot
  hold a runner, or a token, for the six-hour default;
* the manual packaging workflows run only on ``workflow_dispatch`` with
  read-only tokens and check out the exact candidate commit;
* every workflow a release tag push triggers runs the release gate
  (``.github/scripts/release_gate.py``) before any job of it can upload or
  publish, with read scopes only, and the token reaches only the gate's
  read-only ``gh api`` fetch.

``test_release_lock.py`` holds every install under a write token to the
hash-locked release lock.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.ci.verify_desktop_production_gate import ROOT

WORKFLOW_DIRECTORY = ROOT / ".github/workflows"
RELEASE_GATE = ".github/scripts/release_gate.py"
TAG_PUSH = "github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')"
#: Actions that upload, attest or publish something.
_PUBLISHING_ACTIONS = (
    "actions/upload-artifact@",
    "actions/attest@",
    "pypa/gh-action-pypi-publish@",
)
_GH_CALL = re.compile(r"(^|[\s;&|(])gh\s+(\S+)(.*)")
#: gh api options that turn a read into a write.
_GH_WRITE_OPTIONS = re.compile(
    r"(^|\s)(-X|--method|-f|-F|--field|--raw-field|--input)(\s|=|$)"
)

#: The dispatch-only packaging workflows and the exact read scopes each holds.
MANUAL_PACKAGING_WORKFLOWS = {
    "desktop-archive.yml": {"contents": "read"},
    "desktop-rpm.yml": {"actions": "read", "contents": "read"},
    "desktop-python-rpms.yml": {"contents": "read"},
}


def _workflow_paths() -> list[Path]:
    return sorted(
        path
        for pattern in ("*.yml", "*.yaml")
        for path in WORKFLOW_DIRECTORY.glob(pattern)
    )


@pytest.fixture(scope="module")
def workflows() -> dict[str, dict[str, Any]]:
    return {path.name: yaml.safe_load(path.read_text()) for path in _workflow_paths()}


def _jobs(
    workflows: dict[str, dict[str, Any]],
) -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (name, job_id, job)
        for name, document in workflows.items()
        for job_id, job in document["jobs"].items()
    ]


def test_every_checkout_drops_its_credentials(
    workflows: dict[str, dict[str, Any]],
) -> None:
    checkouts = 0
    for name, job_id, job in _jobs(workflows):
        for index, step in enumerate(job.get("steps", [])):
            if not str(step.get("uses", "")).startswith("actions/checkout@"):
                continue
            checkouts += 1
            persist = (step.get("with") or {}).get("persist-credentials")
            assert persist is False, (
                f"{name}:{job_id}:{index} keeps the token in .git/config"
            )
    assert checkouts


def test_no_workflow_runs_on_pull_request_target(
    workflows: dict[str, dict[str, Any]],
) -> None:
    # pull_request_target runs fork code with the base repository's secrets.
    # PyYAML reads the bare `on` key as True.
    for name, document in workflows.items():
        triggers = document.get(True) or document.get("on") or {}
        if isinstance(triggers, str):
            triggers = {triggers: None}
        elif isinstance(triggers, list):
            triggers = dict.fromkeys(triggers)
        assert "pull_request_target" not in triggers, (
            f"{name} runs on pull_request_target"
        )


def test_every_file_declares_its_permissions(
    workflows: dict[str, dict[str, Any]],
) -> None:
    for name, document in workflows.items():
        if "permissions" in document:
            continue
        undeclared = [
            job_id
            for job_id, job in document["jobs"].items()
            if "permissions" not in job
        ]
        assert not undeclared, f"{name} leaves permissions implicit for {undeclared}"


def test_every_job_has_a_timeout(workflows: dict[str, dict[str, Any]]) -> None:
    for name, job_id, job in _jobs(workflows):
        label = f"{name}:{job_id}"
        if "uses" in job:
            # GitHub refuses timeout-minutes on a job that calls a reusable
            # workflow.  The called file's own jobs carry the timeouts, and
            # this scan covers them because the called file is local.
            called = str(job["uses"])
            assert called.startswith("./.github/workflows/"), label
            assert Path(called).name in workflows, label
            assert "timeout-minutes" not in job, label
            continue
        timeout = job.get("timeout-minutes")
        assert isinstance(timeout, int) and not isinstance(timeout, bool), label
        assert 0 < timeout <= 180, f"{label} timeout {timeout}"


@pytest.mark.parametrize("name", sorted(MANUAL_PACKAGING_WORKFLOWS))
def test_manual_packaging_workflows_are_dispatch_only_and_read_only(
    workflows: dict[str, dict[str, Any]], name: str
) -> None:
    document = workflows[name]
    # PyYAML reads the bare ``on`` key as the boolean True.
    triggers = document.get("on", document.get(True))
    assert set(triggers) == {"workflow_dispatch"}, name
    assert document["permissions"] == MANUAL_PACKAGING_WORKFLOWS[name]
    checkouts = 0
    for job_id, job in document["jobs"].items():
        assert "permissions" not in job, f"{name}:{job_id} widens its token"
        for step in job["steps"]:
            if str(step.get("uses", "")).startswith("actions/checkout@"):
                checkouts += 1
                assert step["with"]["ref"] == "${{ env.TONGS_HEAD_SHA }}", name
    assert checkouts, name


def _triggers(document: dict[str, Any]) -> dict[str, Any]:
    triggers = document.get("on", document.get(True)) or {}
    if isinstance(triggers, str):
        return {triggers: None}
    if isinstance(triggers, list):
        return dict.fromkeys(triggers)
    return triggers


def _runs_gate(step: dict[str, Any]) -> bool:
    return RELEASE_GATE in (step.get("run") or "")


def _publishes(step: dict[str, Any]) -> bool:
    uses = str(step.get("uses", ""))
    run = " ".join((step.get("run") or "").split())
    return uses.startswith(_PUBLISHING_ACTIONS) or "gh release" in run


def _gated_jobs(document: dict[str, Any]) -> set[str]:
    """Return the jobs that cannot run a step unless the gate passed.

    A job is gated when it runs the gate unconditionally for a tag push, or
    when it needs a gated job and its condition cannot bypass that job's
    failure.
    """

    jobs = document["jobs"]
    gated = set()
    for job_id, job in jobs.items():
        for step in job.get("steps", []):
            if _runs_gate(step):
                condition = " ".join(str(step.get("if", TAG_PUSH)).split())
                if condition == TAG_PUSH:
                    gated.add(job_id)
    changed = True
    while changed:
        changed = False
        for job_id, job in jobs.items():
            if job_id in gated:
                continue
            needs = job.get("needs") or []
            needs = [needs] if isinstance(needs, str) else needs
            condition = " ".join(str(job.get("if", "")).split())
            if any(
                word in condition for word in ("always()", "failure()", "cancelled()")
            ):
                continue
            for need in needs:
                if need in gated and (
                    not condition or f"needs.{need}.result == 'success'" in condition
                ):
                    gated.add(job_id)
                    changed = True
                    break
    return gated


def test_every_tag_push_workflow_gates_what_it_publishes(
    workflows: dict[str, dict[str, Any]],
) -> None:
    tag_workflows = 0
    for name, document in workflows.items():
        push = _triggers(document).get("push") or {}
        if not push.get("tags"):
            continue
        tag_workflows += 1
        gated = _gated_jobs(document)
        assert gated, f"{name} runs on a tag push without the release gate"
        for job_id, job in document["jobs"].items():
            steps = job.get("steps", [])
            publishing = [index for index, step in enumerate(steps) if _publishes(step)]
            if not publishing:
                continue
            label = f"{name}:{job_id}"
            assert job_id in gated, f"{label} can publish without the release gate"
            gate = [index for index, step in enumerate(steps) if _runs_gate(step)]
            if gate:
                assert max(gate) < min(publishing), label
    # publish.yml and release-desktop.yml.
    assert tag_workflows == 2


def test_the_release_gate_token_is_read_only_and_reaches_only_its_fetch(
    workflows: dict[str, dict[str, Any]],
) -> None:
    gate_jobs = 0
    for name, document in workflows.items():
        for job_id, job in document["jobs"].items():
            steps = job.get("steps", [])
            gate = [index for index, step in enumerate(steps) if _runs_gate(step)]
            if not gate:
                continue
            gate_jobs += 1
            label = f"{name}:{job_id}"
            assert job["permissions"] == {"contents": "read", "actions": "read"}, label
            assert "GH_TOKEN" not in (document.get("env") or {}), label
            assert "GH_TOKEN" not in (job.get("env") or {}), label
            assert "GH_TOKEN" not in (steps[gate[0]].get("env") or {}), label
            fetches = 0
            for step in steps:
                script = (step.get("run") or "").replace("\\\n", " ")
                calls = [
                    match
                    for line in script.splitlines()
                    if (match := _GH_CALL.search(line.split("#", 1)[0]))
                ]
                # Only steps that call gh hold the token, and every call there
                # is a gh api read.
                has_token = "GH_TOKEN" in (step.get("env") or {})
                assert has_token == bool(calls), (label, step.get("name"))
                fetches += bool(calls)
                for call in calls:
                    assert call.group(2) == "api", (label, call.group(0))
                    assert not _GH_WRITE_OPTIONS.search(call.group(3)), (
                        label,
                        call.group(0),
                    )
            assert fetches == 1, label
    # publish.yml build and release-desktop.yml candidate-archive.
    assert gate_jobs == 2
