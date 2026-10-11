"""Run every consumer of the RPM builder pin against fake Podman and gh.

The desktop RPM harness, the manual companion RPM harness and the Fedora 44
probe read ``packaging/rpm/builder/image.json``.  Each case copies one
consumer and the pin tool into a throwaway git repository, puts recording
``podman`` and ``gh`` programs first on ``PATH``, and stops the run at the
first ``podman build``, so the order of the pin check, the attestation check,
the digest pulls and the first build is observed without a container runtime.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[4]
PIN_TOOL = "scripts/pin_rpm_builder.py"
PIN = "packaging/rpm/builder/image.json"
DESKTOP_HARNESS = "packaging/rpm/desktop/run_hosted.sh"
COMPANION_HARNESS = "packaging/rpm/python-dependencies/run_hosted.sh"
PROBE = "tests/containers/run-fedora-44.sh"
PROBE_INPUTS = (
    PROBE,
    "tests/containers/Containerfile",
    "tests/containers/probe.py",
    "requirements/installer-verifier.lock",
)

IMAGE = "ghcr.io/andre-motta/tongs-rpm-builder"
DIGEST = "sha256:" + "a" * 64
BASE_DIGEST = "sha256:" + "b" * 64
SOURCE_COMMIT = "c" * 40
BUILDER = f"{IMAGE}@{DIGEST}"
BASE = f"registry.fedoraproject.org/fedora@{BASE_DIGEST}"
LOCAL_BUILDER = "localhost/tongs-rpm-builder:local"
#: The fake ``podman build`` status; a run that reaches the first build exits
#: with it.
FIRST_BUILD = 97

PUBLISHED = f"""{{
  "image": "{IMAGE}",
  "digest": "{DIGEST}",
  "tag": "fedora44-20261011-{SOURCE_COMMIT[:12]}",
  "base_digest": "{BASE_DIGEST}",
  "source_commit": "{SOURCE_COMMIT}",
  "run_id": 123,
  "run_url": "https://github.com/andre-motta/tongs/actions/runs/123"
}}
"""
UNPUBLISHED = f"""{{
  "image": "{IMAGE}",
  "digest": null,
  "tag": null,
  "base_digest": null,
  "source_commit": null,
  "run_id": null,
  "run_url": null
}}
"""

FAKE_PODMAN = r"""#!/usr/bin/env bash
printf 'podman %s\n' "$*" >>"$FAKE_LOG"
case "$1" in
    version|info) printf '{}\n' ;;
    pull) printf 'sha256:%064d\n' 0 ;;
    image)
        case "$2" in
            exists) exit "${FAKE_IMAGE_EXISTS:-0}" ;;
            inspect)
                if [[ "$3" == --format ]]; then
                    case "$4" in
                        *image.base.digest*) printf '%s\n' "${FAKE_BASE_LABEL:-}" ;;
                        *image.revision*) printf '%s\n' "${FAKE_REVISION_LABEL:-}" ;;
                        *) printf '\n' ;;
                    esac
                else
                    printf '[]\n'
                fi
                ;;
            *) exit 98 ;;
        esac
        ;;
    run) ;;
    build) exit 97 ;;
    *) exit 98 ;;
esac
"""
FAKE_GH = r"""#!/usr/bin/env bash
printf 'gh %s\n' "$*" >>"$FAKE_LOG"
[[ "$1 $2" == "attestation verify" ]] || exit 98
printf '[]\n'
exit "${FAKE_GH_STATUS:-0}"
"""


@dataclass(frozen=True)
class Run:
    returncode: int
    stderr: str
    calls: list[str]
    output: Path


def _git(repo: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        capture_output=True,
        text=True,
        check=True,
        env={
            "PATH": os.environ["PATH"],
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Tongs",
            "GIT_AUTHOR_EMAIL": "tongs@example.invalid",
            "GIT_COMMITTER_NAME": "Tongs",
            "GIT_COMMITTER_EMAIL": "tongs@example.invalid",
        },
    )
    return completed.stdout.strip()


def _run(
    tmp_path: Path,
    program: str,
    *,
    pin: str,
    inputs: tuple[str, ...] = (),
    arguments: tuple[str, ...] = (),
    hosted: bool = True,
    environment: dict[str, str] | None = None,
) -> Run:
    repo = tmp_path / "repo"
    for relative in (program, PIN_TOOL, *inputs):
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    (repo / PIN).parent.mkdir(parents=True, exist_ok=True)
    (repo / PIN).write_text(pin)
    _git(repo, "init", "--quiet")
    _git(repo, "add", "--all")
    _git(repo, "commit", "--quiet", "--message", "fixture")
    head = _git(repo, "rev-parse", "HEAD")

    fakes = tmp_path / "bin"
    fakes.mkdir()
    for name, text in (("podman", FAKE_PODMAN), ("gh", FAKE_GH)):
        (fakes / name).write_text(text)
        (fakes / name).chmod(0o755)
    log = tmp_path / "calls.log"
    log.touch()
    output = tmp_path / "output"
    accepted = tmp_path / "accepted"
    accepted.mkdir()
    (tmp_path / "contract.json").write_text("{}\n")

    env = {
        "PATH": f"{fakes}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "TONGS_HEAD_SHA": head,
        "FAKE_LOG": str(log),
        "FAKE_BASE_LABEL": BASE_DIGEST,
        "FAKE_REVISION_LABEL": SOURCE_COMMIT,
    }
    if hosted:
        env |= {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted"}
    env |= environment or {}
    command = [
        "bash",
        str(repo / program),
        "--output-dir",
        str(output),
        *(
            word.format(accepted=accepted, contract=tmp_path / "contract.json")
            for word in arguments
        ),
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        env=env,
        cwd=tmp_path,
        timeout=60,
    )
    return Run(
        completed.returncode, completed.stderr, log.read_text().splitlines(), output
    )


ACCEPTED = ("--accepted-dir", "{accepted}", "--payload-manifest", "{contract}")


def _index(calls: list[str], prefix: str) -> int:
    return next(index for index, call in enumerate(calls) if call.startswith(prefix))


def _builds(calls: list[str]) -> list[str]:
    return [call for call in calls if call.startswith("podman build")]


def test_the_desktop_harness_refuses_an_unpublished_pin(tmp_path: Path) -> None:
    run = _run(tmp_path, DESKTOP_HARNESS, pin=UNPUBLISHED, arguments=ACCEPTED)

    assert run.returncode == 1
    assert "not published yet" in run.stderr
    assert run.calls == ["podman version --format json", "podman info --format json"]


def test_the_desktop_harness_verifies_then_pulls_by_digest_before_building(
    tmp_path: Path,
) -> None:
    run = _run(tmp_path, DESKTOP_HARNESS, pin=PUBLISHED, arguments=ACCEPTED)

    assert run.returncode == FIRST_BUILD, run.stderr
    verify = _index(run.calls, "gh attestation verify")
    assert run.calls[verify] == (
        f"gh attestation verify oci://{BUILDER} --repo andre-motta/tongs "
        "--signer-workflow andre-motta/tongs/.github/workflows/rpm-builder-image.yml "
        f"--source-ref refs/heads/main --source-digest {SOURCE_COMMIT} "
        "--deny-self-hosted-runners --format json"
    )
    pull_builder = _index(run.calls, f"podman pull --quiet {BUILDER}")
    pull_base = _index(run.calls, f"podman pull --quiet {BASE}")
    first_build = _index(run.calls, "podman build")
    assert verify < pull_builder < pull_base < first_build
    pulls = [call for call in run.calls if call.startswith("podman pull")]
    assert pulls == [f"podman pull --quiet {BUILDER}", f"podman pull --quiet {BASE}"]
    assert f"--build-arg BASE_IMAGE={BUILDER} " in _builds(run.calls)[0]
    # The pin and the verification output join the evidence the checksum
    # inventory and the receipt bind to this run.
    assert (run.output / "builder-pin.json").read_text() == PUBLISHED
    assert (run.output / "builder-attestation.json").read_text() == "[]\n"
    assert (run.output / "builder-image.inspect.json").is_file()
    assert "builder_mode=pinned" in (run.output / "runner.env").read_text()


def test_the_desktop_harness_builds_nothing_when_the_attestation_fails(
    tmp_path: Path,
) -> None:
    run = _run(
        tmp_path,
        DESKTOP_HARNESS,
        pin=PUBLISHED,
        arguments=ACCEPTED,
        environment={"FAKE_GH_STATUS": "1"},
    )

    assert run.returncode == 1
    assert any(call.startswith("gh attestation verify") for call in run.calls)
    assert not any(
        call.startswith(("podman pull", "podman build")) for call in run.calls
    )


@pytest.mark.parametrize(
    ("labels", "message"),
    [
        ({"FAKE_BASE_LABEL": "sha256:" + "d" * 64}, "pinned Fedora base"),
        ({"FAKE_REVISION_LABEL": "e" * 40}, "revision label differs"),
    ],
)
def test_the_desktop_harness_refuses_a_builder_that_disagrees_with_the_pin(
    tmp_path: Path, labels: dict[str, str], message: str
) -> None:
    run = _run(
        tmp_path,
        DESKTOP_HARNESS,
        pin=PUBLISHED,
        arguments=ACCEPTED,
        environment=labels,
    )

    assert run.returncode == 1
    assert message in run.stderr
    assert not _builds(run.calls)
    assert not any(call.startswith(f"podman pull --quiet {BASE}") for call in run.calls)


def test_the_local_builder_mode_uses_the_local_image_and_its_base(
    tmp_path: Path,
) -> None:
    run = _run(
        tmp_path,
        DESKTOP_HARNESS,
        pin=UNPUBLISHED,
        arguments=(*ACCEPTED, "--local-builder", LOCAL_BUILDER),
        hosted=False,
    )

    assert run.returncode == FIRST_BUILD, run.stderr
    assert not any(call.startswith("gh ") for call in run.calls)
    pulls = [call for call in run.calls if call.startswith("podman pull")]
    assert pulls == [f"podman pull --quiet {BASE}"]
    assert f"--build-arg BASE_IMAGE={LOCAL_BUILDER} " in _builds(run.calls)[0]
    assert "builder_mode=local" in (run.output / "runner.env").read_text()
    assert (run.output / "builder-local.txt").is_file()
    assert not (run.output / "builder-pin.json").exists()


@pytest.mark.parametrize(
    "environment",
    [
        {"GITHUB_ACTIONS": "true"},
        {"GITHUB_ACTIONS": ""},
        {"RUNNER_ENVIRONMENT": "github-hosted"},
    ],
)
def test_the_local_builder_mode_is_refused_inside_a_workflow(
    tmp_path: Path, environment: dict[str, str]
) -> None:
    run = _run(
        tmp_path,
        DESKTOP_HARNESS,
        pin=PUBLISHED,
        arguments=(*ACCEPTED, "--local-builder", LOCAL_BUILDER),
        hosted=False,
        environment=environment,
    )

    assert run.returncode == 2
    assert "local builder mode is refused" in run.stderr
    assert run.calls == []


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (
            (*ACCEPTED, "--local-builder", BUILDER),
            "must be a localhost/ image reference",
        ),
        (
            (*ACCEPTED, "--local-builder", "localhost/x:1 --pull"),
            "must be a localhost/ image reference",
        ),
        (("--local-builder", LOCAL_BUILDER), "needs --accepted-dir"),
    ],
)
def test_the_local_builder_mode_takes_only_a_local_image_and_a_local_archive(
    tmp_path: Path, arguments: tuple[str, ...], message: str
) -> None:
    run = _run(
        tmp_path, DESKTOP_HARNESS, pin=PUBLISHED, arguments=arguments, hosted=False
    )

    assert run.returncode == 2
    assert message in run.stderr
    assert run.calls == []


def test_the_local_builder_mode_requires_the_image_to_exist(tmp_path: Path) -> None:
    run = _run(
        tmp_path,
        DESKTOP_HARNESS,
        pin=UNPUBLISHED,
        arguments=(*ACCEPTED, "--local-builder", LOCAL_BUILDER),
        hosted=False,
        environment={"FAKE_IMAGE_EXISTS": "1"},
    )

    assert run.returncode == 2
    assert "local builder image not found" in run.stderr
    assert not _builds(run.calls)


def test_the_hosted_mode_still_requires_a_github_hosted_runner(
    tmp_path: Path,
) -> None:
    run = _run(
        tmp_path, DESKTOP_HARNESS, pin=PUBLISHED, arguments=ACCEPTED, hosted=False
    )

    assert run.returncode == 2
    assert "GitHub-hosted disposable runner" in run.stderr
    assert run.calls == []


@pytest.mark.parametrize(
    ("program", "inputs"),
    [(COMPANION_HARNESS, ()), (PROBE, PROBE_INPUTS[1:])],
)
def test_plain_fedora_consumers_refuse_an_unpublished_pin(
    tmp_path: Path, program: str, inputs: tuple[str, ...]
) -> None:
    run = _run(tmp_path, program, pin=UNPUBLISHED, inputs=inputs)

    assert run.returncode == 1
    assert "not published yet" in run.stderr
    assert not any(
        call.startswith(("podman pull", "podman build")) for call in run.calls
    )


@pytest.mark.parametrize(
    ("program", "inputs"),
    [(COMPANION_HARNESS, ()), (PROBE, PROBE_INPUTS[1:])],
)
def test_plain_fedora_consumers_build_on_the_pinned_base_digest(
    tmp_path: Path, program: str, inputs: tuple[str, ...]
) -> None:
    run = _run(tmp_path, program, pin=PUBLISHED, inputs=inputs)

    assert run.returncode == FIRST_BUILD, run.stderr
    # Plain Fedora only: neither consumer pulls or verifies the builder image.
    assert not any(call.startswith("gh ") for call in run.calls)
    pulls = [call for call in run.calls if call.startswith("podman pull")]
    assert pulls == [f"podman pull --quiet {BASE}"]
    assert f"--build-arg BASE_IMAGE={BASE} " in _builds(run.calls)[0]


def test_the_probe_names_no_moving_fedora_tag() -> None:
    for relative in PROBE_INPUTS[:2]:
        assert "fedora:44" not in (ROOT / relative).read_text(), relative
    containerfile = (ROOT / "tests/containers/Containerfile").read_text()
    assert re.findall(r"^FROM\s+(\S+)", containerfile, re.MULTILINE) == [
        "${BASE_IMAGE}"
    ]
    assert re.findall(r"^ARG BASE_IMAGE.*$", containerfile, re.MULTILINE) == [
        "ARG BASE_IMAGE"
    ]
