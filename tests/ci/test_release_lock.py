"""Hold the jobs that carry a signing or write token to hash-locked installs.

A job holding ``id-token: write`` or ``contents: write`` runs everything it
installs while that token is available.  A package resolved fresh from the
index at release time, or an unpinned build backend pulled in by build
isolation, would therefore run with the token.  These cases read every
workflow and require each such job to install only from a hash-locked
requirements file, and then the source tree itself without resolving
anything.  The tag-path jobs that build what the signing and publish jobs
then attest or upload as is hold the same line without holding a token.  The
PyPI build job and the installed-core fixture wheel install only the lock and
build without isolation.  Installs are found with the scanner in
``workflow_installs.py``.  They also hold ``requirements/release.lock`` to its
contract: every entry pinned exactly and hashed, covering the core runtime
dependencies, the signing scripts' imports and the build backend.  The Fedora
44 probe installs its Sigstore verifier from ``requirements/installer-verifier.lock``
by hash, and only then its test tools, constrained to the locked versions.
Finally, ``GH_TOKEN`` may reach only the steps of those jobs that actually call
``gh``.
"""

from __future__ import annotations

import itertools
import re
import tomllib
from typing import Any

import pytest
import yaml
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

from tests.ci.verify_desktop_production_gate import ROOT
from tests.ci.workflow_installs import (
    COMMAND_SEPARATORS,
    pip_installs,
    shell_tokens,
)
from tests.ci.workflow_installs import install_commands as _install_commands

WORKFLOW_DIRECTORY = ROOT / ".github/workflows"
RELEASE_WORKFLOW = WORKFLOW_DIRECTORY / "release-desktop.yml"
RELEASE_LOCK = "requirements/release.lock"
RELEASE_INPUTS = "requirements/release.in"
PUBLICATION_PROGRAM = "tests/integration/desktop/release_publication.py"
VERIFIER_LOCK = "requirements/installer-verifier.lock"
PROBE_CONTAINERFILE = "tests/containers/Containerfile"
PROBE_RUNNER = "tests/containers/run-fedora-44.sh"
PROBE_LOCK = "/opt/tongs-harness/installer-verifier.lock"
PROBE_CONSTRAINTS = "/opt/tongs-harness/installer-verifier.constraints"

#: The release jobs that must install from the lock.  Listing them keeps the
#: generic scan below from passing vacuously if a job is renamed.  The signing
#: and publish jobs hold a token; the archive and RPM jobs on the tag path hold
#: none, but produce what those jobs attest and publish as is.
LOCKED_JOBS = (
    "release-desktop.yml:candidate-archive",
    "release-desktop.yml:candidate-attestation",
    "release-desktop.yml:release-publish",
    "release-desktop.yml:release-rpm",
)

#: Jobs that hold no token but build what a privileged job publishes, or what
#: an audit trusts, as is.  They install only the lock, then build without
#: isolation, so the PyPI wheel and sdist and the installed-core fixture wheel
#: are built by hash-locked code alone.
LOCKED_BUILD_JOBS = ("desktop-production.yml:installed-core", "publish.yml:build")

#: pip flags a privileged install may carry.  Anything else, including a bare
#: requirement, fails the scan.
ALLOWED_INSTALL_FLAGS = frozenset(
    {
        "--isolated",
        "--disable-pip-version-check",
        "--require-hashes",
        "--only-binary=:all:",
        "--no-deps",
        "--no-build-isolation",
    }
)

_LOCK_ENTRY = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s\\;]+)\s*\\?$")
_HASH = re.compile(r"^\s+--hash=sha256:[0-9a-f]{64}\s*\\?$")
_MODULE_BUILD = re.compile(r"-m\s+build(\s|$)")
_GH_COMMAND = re.compile(r"(^|[\s;&|(])gh\s")
_GH_PROGRAM_COMMANDS = ("require-absent", "verify-published")


def _load_workflows() -> dict[str, dict[str, Any]]:
    workflows = {}
    paths = [*WORKFLOW_DIRECTORY.glob("*.yml"), *WORKFLOW_DIRECTORY.glob("*.yaml")]
    for path in sorted(paths):
        workflows[path.name] = yaml.safe_load(path.read_text())
    return workflows


@pytest.fixture(scope="module")
def workflows() -> dict[str, dict[str, Any]]:
    return _load_workflows()


def _write_scopes(permissions: Any) -> set[str]:
    if permissions == "write-all":
        return {"write-all"}
    if isinstance(permissions, dict):
        return {scope for scope, level in permissions.items() if level == "write"}
    return set()


def _privileged_jobs(
    workflows: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Return every job holding a write or OIDC token, as ``<file>:<job>``."""

    found = {}
    for name, document in workflows.items():
        default = document.get("permissions")
        for job_id, job in document["jobs"].items():
            scopes = _write_scopes(job.get("permissions", default))
            if scopes:
                found[f"{name}:{job_id}"] = job
    return found


def _classify_install(tokens: list[str]) -> str:
    """Return ``locked`` or ``source``, or fail for any other install shape."""

    arguments = tokens[tokens.index("install") + 1 :]
    flags = set()
    targets = []
    iterator = iter(arguments)
    for token in iterator:
        if token in ("-r", "-e"):
            targets.append((token, next(iterator)))
        elif token in ALLOWED_INSTALL_FLAGS:
            flags.add(token)
        else:
            raise AssertionError(f"unexpected install argument {token!r} in {tokens}")
    if targets == [("-r", RELEASE_LOCK)]:
        assert {"--require-hashes", "--only-binary=:all:"} <= flags, tokens
        assert "--no-deps" not in flags, tokens
        return "locked"
    if targets == [("-e", ".")]:
        assert {"--no-deps", "--no-build-isolation"} <= flags, tokens
        return "source"
    raise AssertionError(f"install target is neither the lock nor the source: {tokens}")


def _job(workflows: dict[str, dict[str, Any]], label: str) -> dict[str, Any]:
    name, job_id = label.split(":", 1)
    return workflows[name]["jobs"][job_id]


def test_every_privileged_install_is_hash_locked_or_resolves_nothing(
    workflows: dict[str, dict[str, Any]],
) -> None:
    privileged = _privileged_jobs(workflows)
    checked = {label: _job(workflows, label) for label in LOCKED_JOBS}
    checked.update(privileged)
    for label, job in checked.items():
        commands = _install_commands(job)
        if label not in LOCKED_JOBS:
            # docs.yml deploy and publish.yml publish install nothing.  A new
            # install there must adopt the lock and join LOCKED_JOBS.
            assert not commands, f"{label} installs under a write token"
            continue
        # The shared scanner returns installs in step order, so the source
        # install follows the locked one, which supplies its build backend.
        kinds = [_classify_install(tokens) for tokens in commands]
        assert kinds == ["locked", "source"], label


def test_the_published_build_installs_only_the_lock(
    workflows: dict[str, dict[str, Any]],
) -> None:
    for label in LOCKED_BUILD_JOBS:
        job = _job(workflows, label)
        kinds = [_classify_install(tokens) for tokens in _install_commands(job)]
        assert kinds == ["locked"], label
        builds = _build_commands(job)
        assert len(builds) == 1, (label, builds)
        assert builds[0][:5] == ["python", "-I", "-m", "build", "--no-isolation"], (
            label,
            builds,
        )


def _build_commands(job: dict[str, Any]) -> list[list[str]]:
    """Return every ``-m build`` command in the job's steps, tokenized."""

    commands = []
    for step in job.get("steps", []):
        script = (step.get("run") or "").replace("\\\n", " ")
        for line in script.splitlines():
            if not _MODULE_BUILD.search(line):
                continue
            command: list[str] = []
            for token in [*shell_tokens(line), ";"]:
                if token not in COMMAND_SEPARATORS:
                    command.append(token)
                    continue
                if ("-m", "build") in itertools.pairwise(command):
                    commands.append(command)
                command = []
    return commands


@pytest.mark.parametrize(
    "script",
    [
        "pip3 install build",
        "python -m pip  install build",
        "python3 -m pip install\\\n  build",
        "cd src && python -m pip install --no-deps -e .",
        "echo ok; /usr/bin/pip install sigstore",
        "/usr/bin/pip3 install sigstore",
    ],
)
def test_the_scan_sees_every_spelling_of_pip_install(script: str) -> None:
    commands = _install_commands({"steps": [{"run": script}]})
    assert len(commands) == 1
    with pytest.raises(AssertionError):
        _classify_install(commands[0])


def test_the_install_scan_ignores_comments_and_splits_commands() -> None:
    job = {
        "steps": [
            {
                "run": (
                    "# Don't pip install anything unpinned here\n"
                    "echo ready  # a later pip install would need hashes\n"
                    "cd src && python -m pip install --require-hashes -r x.lock"
                    " && pip3 install ruff\n"
                )
            }
        ]
    }
    assert _install_commands(job) == [
        ["python", "-m", "pip", "install", "--require-hashes", "-r", "x.lock"],
        ["pip3", "install", "ruff"],
    ]


def _calls_gh(step: dict[str, Any]) -> bool:
    run = " ".join((step.get("run") or "").split())
    if _GH_COMMAND.search(run):
        return True
    return any(
        f"{PUBLICATION_PROGRAM} {command}" in run for command in _GH_PROGRAM_COMMANDS
    )


def test_gh_token_reaches_only_the_steps_that_call_gh(
    workflows: dict[str, dict[str, Any]],
) -> None:
    for name, document in workflows.items():
        assert "GH_TOKEN" not in (document.get("env") or {}), name
    calling = 0
    for label, job in _privileged_jobs(workflows).items():
        assert "GH_TOKEN" not in (job.get("env") or {}), label
        for step in job.get("steps", []):
            has_token = "GH_TOKEN" in (step.get("env") or {})
            assert has_token == _calls_gh(step), (label, step.get("name"))
            if has_token:
                assert step["env"]["GH_TOKEN"] == "${{ github.token }}"
                calling += 1
    # require-absent, create, draft check, edit and the final check of the
    # release, and the builder workflow's pin pull request.
    assert calling == 6


def _lock_entries(lock: str = RELEASE_LOCK) -> dict[str, tuple[Version, int]]:
    """Parse a lock into ``{canonical name: (version, hash count)}``."""

    entries: dict[str, tuple[Version, int]] = {}
    current = None
    for line in (ROOT / lock).read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0].isspace():
            assert current is not None, line
            assert _HASH.match(line), line
            version, hashes = entries[current]
            entries[current] = (version, hashes + 1)
            continue
        match = _LOCK_ENTRY.match(line)
        assert match, f"lock entry is not an exact pin: {line!r}"
        current = canonicalize_name(match.group(1))
        assert current not in entries, current
        entries[current] = (Version(match.group(2)), 0)
    return entries


@pytest.mark.parametrize("lock", [RELEASE_LOCK, VERIFIER_LOCK])
def test_every_lock_entry_is_pinned_and_hashed(lock: str) -> None:
    entries = _lock_entries(lock)
    assert entries
    for name, (_, hashes) in entries.items():
        assert hashes >= 1, f"{name} carries no hash"


def test_the_lock_matches_the_release_python_and_sigstore(
    workflows: dict[str, dict[str, Any]],
) -> None:
    environment = workflows[RELEASE_WORKFLOW.name]["env"]
    assert environment["PYTHON_VERSION"] == "3.12"
    assert "--python-version 3.12" in (ROOT / RELEASE_LOCK).read_text()
    version, _ = _lock_entries()["sigstore"]
    assert version == Version(environment["SIGSTORE_VERSION"])


def test_the_lock_covers_the_core_the_build_backend_and_the_signing_imports() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    entries = _lock_entries()
    required = [Requirement(item) for item in pyproject["project"]["dependencies"]]
    required += [Requirement(item) for item in pyproject["build-system"]["requires"]]
    # The SPDX generator in the signing job imports jsonschema, bounded as the
    # dev extra bounds it, and hatchling builds the editable wheel with
    # editables.
    required += [
        Requirement(item)
        for item in pyproject["project"]["optional-dependencies"]["dev"]
        if Requirement(item).name == "jsonschema"
    ]
    required.append(Requirement("editables"))
    names = {canonicalize_name(item.name) for item in required}
    assert {"sigstore", "jsonschema", "hatchling", "hatch-vcs"} <= names
    for requirement in required:
        name = canonicalize_name(requirement.name)
        assert name in entries, f"{name} is missing from {RELEASE_LOCK}"
        version, _ = entries[name]
        assert requirement.specifier.contains(version, prereleases=True), (
            f"{name} {version} does not satisfy {requirement}"
        )


def test_every_release_input_is_in_the_lock() -> None:
    entries = _lock_entries()
    inputs = [
        Requirement(line)
        for line in (ROOT / RELEASE_INPUTS).read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert inputs
    for requirement in inputs:
        name = canonicalize_name(requirement.name)
        assert name in entries, name
        assert requirement.specifier.contains(entries[name][0], prereleases=True)


def _probe_install_runs() -> list[str]:
    """Return each ``RUN`` instruction of the probe image, continuations joined."""

    runs = []
    instruction = ""
    for line in (ROOT / PROBE_CONTAINERFILE).read_text().splitlines():
        if not instruction and (not line.strip() or line.lstrip().startswith("#")):
            continue
        if line.rstrip().endswith("\\"):
            instruction += line.rstrip()[:-1] + " "
            continue
        instruction += line
        if instruction.startswith("RUN "):
            runs.append(instruction[len("RUN ") :])
        instruction = ""
    return runs


def test_the_probe_installs_its_verifier_by_hash_before_any_test_tool() -> None:
    runner = (ROOT / PROBE_RUNNER).read_text()
    assert f'"$repo_root/{VERIFIER_LOCK}"' in runner
    containerfile = (ROOT / PROBE_CONTAINERFILE).read_text()
    assert f"COPY installer-verifier.lock {PROBE_LOCK}" in containerfile
    installs = [
        command
        for run in _probe_install_runs()
        for command in pip_installs(shell_tokens(run))
    ]
    assert len(installs) == 2, installs
    locked, tools = (command[command.index("install") + 1 :] for command in installs)
    # The verifier closure comes from the lock alone, by hash, as wheels only.
    assert {"--require-hashes", "--only-binary=:all:"} <= set(locked), locked
    assert [
        locked[index + 1] for index, token in enumerate(locked) if token == "-r"
    ] == [PROBE_LOCK], locked
    assert not [
        token for token in locked if not token.startswith("-") and token != PROBE_LOCK
    ], locked
    # The test tools resolve afterwards, held to the locked versions by a
    # hash-free constraints file, and never reinstall the lock without hashes.
    assert PROBE_LOCK not in tools, tools
    assert ("-c", PROBE_CONSTRAINTS) in itertools.pairwise(tools), tools
    # The constraints file is derived from the lock before its first use, and
    # the installed versions are then checked against it.
    run = " ".join(_probe_install_runs())
    assert run.index(PROBE_CONSTRAINTS) < run.index(" -c ")
    assert "importlib.metadata" in run
    assert run.rstrip().endswith("-m pip check")


def test_the_verifier_lock_matches_the_probe_python_and_sigstore(
    workflows: dict[str, dict[str, Any]],
) -> None:
    text = (ROOT / VERIFIER_LOCK).read_text()
    assert "--python-version 3.14" in text
    assert "--only-binary :all:" in text
    version, _ = _lock_entries(VERIFIER_LOCK)["sigstore"]
    environment = workflows[RELEASE_WORKFLOW.name]["env"]
    assert version == Version(environment["SIGSTORE_VERSION"])
