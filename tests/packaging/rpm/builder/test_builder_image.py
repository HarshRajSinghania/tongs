"""Keep the RPM builder image in step with the RPM sources it serves.

The builder image preinstalls the harness tool sets and every declared
``BuildRequires``.  ``packaging/rpm/builder/build_requires.py`` derives that
list from the Containerfiles, the rebuild scripts, the companion specs and the
Tongs spec templates, and these cases fail when the committed
``packages.txt`` drifts from them or the Containerfile stops installing it
from a digest-pinned base.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).parents[4]
BUILDER = ROOT / "packaging" / "rpm" / "builder"
PROGRAM = BUILDER / "build_requires.py"


def _load() -> ModuleType:
    specification = importlib.util.spec_from_file_location("build_requires", PROGRAM)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


build_requires = _load()


def _declared_build_requires() -> dict[str, set[str]]:
    """Every BuildRequires name per spec, read independently of the program."""

    specs = sorted((ROOT / "packaging/rpm/python-dependencies/specs").glob("*.spec"))
    specs += sorted((ROOT / "packaging/rpm/desktop/templates").glob("*.spec.in"))
    declared = {}
    for path in specs:
        names = re.findall(r"^BuildRequires:\s+(\S+)", path.read_text(), re.MULTILINE)
        declared[path.name] = set(names)
    return declared


def test_the_committed_package_list_matches_the_sources() -> None:
    assert (BUILDER / "packages.txt").read_text() == build_requires.render(
        build_requires.derive()
    )


def test_check_passes_on_the_runner_python_without_dependencies() -> None:
    completed = subprocess.run(
        [sys.executable, "-I", str(PROGRAM), "--check"],
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr


def test_the_list_covers_every_declared_build_requirement() -> None:
    packages = set(build_requires.derive())
    manifest = json.loads(
        (ROOT / "packaging/rpm/python-dependencies/manifest.json").read_text()
    )
    companions = {
        f"python3dist({item['distribution']})" for item in manifest["companions"]
    }
    declared = _declared_build_requires()
    assert {"python-tongs.spec.in", "tongs-desktop.spec.in"} <= set(declared)
    assert len(declared) == len(manifest["build_order"]) + 2
    for spec, names in declared.items():
        assert names, spec
        assert names - companions <= packages, spec
    # python-tongs builds against the source-built Sigstore, which Fedora does
    # not ship, so the image leaves it to the companion repository.
    assert "python3dist(sigstore)" in declared["python-tongs.spec.in"]
    assert not companions & packages


def test_the_list_covers_the_harness_tool_sets() -> None:
    packages = set(build_requires.derive())
    for relative in build_requires.TOOL_SOURCES:
        text = (ROOT / relative).read_text().replace("\\\n", " ")
        (command,) = [line for line in text.splitlines() if "RUN dnf install" in line]
        words = command.split("&&", 1)[0].split()[3:]
        tools = {word.strip("'") for word in words if not word.startswith("-")}
        assert tools, relative
        assert tools <= packages, relative
    assert {"rpm-build", "dnf5-plugins", "createrepo_c", "cargo", "maturin"} <= (
        packages
    )


def test_the_list_is_sorted_unique_and_free_of_versions() -> None:
    lines = (BUILDER / "packages.txt").read_text().splitlines()
    packages = [line for line in lines if not line.startswith("#")]
    assert packages == sorted(set(packages))
    for name in packages:
        assert " " not in name and ">" not in name and "=" not in name, name


def test_the_containerfile_installs_the_list_on_a_digest_pinned_base() -> None:
    text = (BUILDER / "Containerfile").read_text()
    froms = re.findall(r"^FROM\s+(\S+)", text, re.MULTILINE)
    assert froms == ["registry.fedoraproject.org/fedora:44@${FEDORA_DIGEST}"]
    assert text.index("ARG FEDORA_DIGEST") < text.index("FROM ")
    assert "COPY packages.txt /usr/share/tongs-rpm-builder/packages.txt" in text
    assert "--arg-file=/tmp/packages" in text
    assert "rpm --query --whatprovides" in text
    assert "grep -Eqx 'sha256:[0-9a-f]{64}'" in text
    assert "grep -Eqx '[0-9a-f]{40}'" in text
    # The repository metadata stays so the rebuild layers start warm; only the
    # downloaded packages are dropped.
    assert "dnf clean packages" in text
    assert "dnf clean all" not in text
    assert "--setopt=install_weak_deps=False" in text
    # No package is named outside the generated list.
    joined = text.replace("\\\n", " ")
    installs = [line for line in joined.splitlines() if "dnf install" in line]
    assert len(installs) == 1
    assert installs[0].split("dnf install", 1)[1].split("&&", 1)[0].split() == [
        "--assumeyes",
        "--setopt=install_weak_deps=False",
    ]
    labels = dict(re.findall(r'^LABEL (\S+)="([^"]*)"', text, re.MULTILINE))
    assert labels["org.opencontainers.image.source"] == (
        "https://github.com/andre-motta/tongs"
    )
    assert labels["org.opencontainers.image.revision"] == "${SOURCE_COMMIT}"
    assert labels["org.opencontainers.image.base.digest"] == "${FEDORA_DIGEST}"
    assert labels["org.opencontainers.image.base.name"] == (
        "registry.fedoraproject.org/fedora:44"
    )


def test_the_unpublished_pin_carries_no_digest() -> None:
    pin = json.loads((BUILDER / "image.json").read_text())
    assert pin["image"] == "ghcr.io/andre-motta/tongs-rpm-builder"
    assert pin["digest"] is None or re.fullmatch(r"sha256:[0-9a-f]{64}", pin["digest"])


@pytest.mark.parametrize(
    "line",
    [
        "BuildRequires:  %{py3_dist foo}",
        "BuildRequires:  foo, bar",
        "BuildRequires:  foo >= ",
        "BuildRequires:  (foo or bar)",
    ],
)
def test_an_unsupported_build_requirement_is_refused(line: str) -> None:
    with pytest.raises(ValueError):
        build_requires.build_requires(f"Name: x\n{line}\n", "x.spec")


def test_a_spec_without_build_requirements_is_refused() -> None:
    with pytest.raises(ValueError):
        build_requires.build_requires("Name: x\n", "x.spec")


def test_build_requirements_drop_versions_and_keep_provides() -> None:
    spec = (
        "BuildRequires:  python3-devel >= 3.12\n"
        "BuildRequires:  python3dist(hatchling) >= 1.31\n"
        "BuildRequires:  maturin\n"
    )
    assert build_requires.build_requires(spec, "x.spec") == [
        "python3-devel",
        "python3dist(hatchling)",
        "maturin",
    ]


def test_dnf_install_parsing_stops_at_the_next_command() -> None:
    text = (
        "FROM base\n"
        "RUN dnf install --assumeyes --setopt=install_weak_deps=False \\\n"
        "        git-core \\\n"
        "        'python3dist(hatchling)' \\\n"
        "    && dnf clean all\n"
    )
    assert build_requires.dnf_install_packages(text, "x") == [
        "git-core",
        "python3dist(hatchling)",
    ]
    with pytest.raises(ValueError):
        build_requires.dnf_install_packages("FROM base\n", "x")
    with pytest.raises(ValueError):
        build_requires.dnf_install_packages("RUN dnf install $PACKAGES\n", "x")
