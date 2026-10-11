"""Hold ``scripts/pin_rpm_builder.py`` and the committed pin to their contract.

The builder workflow's pin job rewrites ``packaging/rpm/builder/image.json``
with this program, and the RPM harness will refuse a pin it cannot validate.
These cases cover the canonical output, every refusal, the unchanged-digest
no-op, and that the committed pin passes ``--check``.
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
import yaml

from tests.ci.verify_desktop_production_gate import ROOT

PROGRAM = ROOT / "scripts/pin_rpm_builder.py"
DIGEST = "sha256:" + "a" * 64
BASE_DIGEST = "sha256:" + "b" * 64
COMMIT = "0123456789abcdef0123456789abcdef01234567"
TAG = "fedora44-20261011-0123456789ab"
RUN_ID = 18800000001


def _load() -> ModuleType:
    specification = importlib.util.spec_from_file_location("pin_rpm_builder", PROGRAM)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


pin_rpm_builder = _load()


def _unpublished() -> dict[str, Any]:
    return {
        "image": "ghcr.io/andre-motta/tongs-rpm-builder",
        "digest": None,
        "tag": None,
        "base_digest": None,
        "source_commit": None,
        "run_id": None,
        "run_url": None,
    }


def _published() -> dict[str, Any]:
    return {
        "image": "ghcr.io/andre-motta/tongs-rpm-builder",
        "digest": DIGEST,
        "tag": TAG,
        "base_digest": BASE_DIGEST,
        "source_commit": COMMIT,
        "run_id": RUN_ID,
        "run_url": f"https://github.com/andre-motta/tongs/actions/runs/{RUN_ID}",
    }


def _write_arguments(**overrides: Any) -> list[str]:
    values = {
        "--digest": DIGEST,
        "--tag": TAG,
        "--base-digest": BASE_DIGEST,
        "--source-commit": COMMIT,
        "--run-id": str(RUN_ID),
        **overrides,
    }
    return [item for pair in values.items() for item in pair]


@pytest.fixture
def pin(tmp_path: Path) -> Path:
    path = tmp_path / "image.json"
    path.write_text(pin_rpm_builder.render(_unpublished()))
    return path


def test_the_committed_pin_passes_check() -> None:
    completed = subprocess.run(
        [sys.executable, "-I", str(PROGRAM), "--check"],
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr


def test_the_committed_pin_names_the_image_and_is_canonical() -> None:
    path = ROOT / "packaging/rpm/builder/image.json"
    document = json.loads(path.read_text())
    assert document["image"] == pin_rpm_builder.IMAGE
    assert path.read_text() == pin_rpm_builder.render(document)


def test_an_unpublished_pin_passes_check_but_not_require_published(
    pin: Path,
) -> None:
    assert pin_rpm_builder.main(["--pin", str(pin), "--check"]) == 0
    assert (
        pin_rpm_builder.main(["--pin", str(pin), "--check", "--require-published"]) == 1
    )


def test_writing_a_pin_produces_canonical_json(
    pin: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert pin_rpm_builder.main(["--pin", str(pin), *_write_arguments()]) == 0
    assert capsys.readouterr().out == "changed=true\n"
    text = pin.read_text()
    assert json.loads(text) == _published()
    assert text == json.dumps(_published(), indent=2) + "\n"
    assert list(json.loads(text)) == list(pin_rpm_builder.FIELDS)
    assert (
        pin_rpm_builder.main(["--pin", str(pin), "--check", "--require-published"]) == 0
    )


def test_the_same_digest_changes_nothing(
    pin: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert pin_rpm_builder.main(["--pin", str(pin), *_write_arguments()]) == 0
    before = pin.read_text()
    capsys.readouterr()
    later = _write_arguments(**{"--run-id": "18800000002"})
    assert pin_rpm_builder.main(["--pin", str(pin), *later]) == 0
    assert capsys.readouterr().out == "changed=false\n"
    assert pin.read_text() == before


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("--digest", "sha256:" + "A" * 64),
        ("--digest", "sha256:" + "a" * 63),
        ("--digest", "sha512:" + "a" * 64),
        ("--digest", "a" * 64),
        ("--digest", "sha256:" + "a" * 64 + "\n"),
        ("--base-digest", "fedora:44"),
        ("--source-commit", COMMIT[:12]),
        ("--tag", "latest"),
        ("--tag", "fedora44-20261332-0123456789ab"),
        ("--tag", "fedora44-20261011-fedcba987654"),
    ],
)
def test_writing_refuses_an_invalid_value(pin: Path, option: str, value: str) -> None:
    before = pin.read_text()
    arguments = _write_arguments(**{option: value})
    assert pin_rpm_builder.main(["--pin", str(pin), *arguments]) == 1
    assert pin.read_text() == before


@pytest.mark.parametrize("run_id", ["0", "-1", "12a"])
def test_writing_refuses_an_invalid_run_id(pin: Path, run_id: str) -> None:
    with pytest.raises(SystemExit) as raised:
        pin_rpm_builder.main(
            ["--pin", str(pin), *_write_arguments(**{"--run-id": run_id})]
        )
    assert raised.value.code == 2


@pytest.mark.parametrize(
    "change",
    [
        {"image": "ghcr.io/someone/tongs-rpm-builder"},
        {"image": "quay.io/andre-motta/tongs-rpm-builder"},
        {"digest": None},
        {"run_id": True},
        {"run_id": "18800000001"},
        {"run_url": "https://github.com/andre-motta/tongs/actions/runs/1"},
        {"extra": "field"},
    ],
)
def test_check_refuses_an_invalid_pin(tmp_path: Path, change: dict[str, Any]) -> None:
    document = {**_published(), **change}
    path = tmp_path / "image.json"
    path.write_text(json.dumps(document, indent=2) + "\n")
    assert pin_rpm_builder.main(["--pin", str(path), "--check"]) == 1


def test_check_refuses_a_pin_that_is_not_canonical(tmp_path: Path) -> None:
    path = tmp_path / "image.json"
    path.write_text(json.dumps(_published(), sort_keys=True))
    assert pin_rpm_builder.main(["--pin", str(path), "--check"]) == 1


def test_check_refuses_unreadable_json(tmp_path: Path) -> None:
    path = tmp_path / "image.json"
    path.write_text("{")
    assert pin_rpm_builder.main(["--pin", str(path), "--check"]) == 1


def test_check_takes_no_values_and_writing_needs_every_value(pin: Path) -> None:
    with pytest.raises(SystemExit) as raised:
        pin_rpm_builder.main(["--pin", str(pin), "--check", "--digest", DIGEST])
    assert raised.value.code == 2
    with pytest.raises(SystemExit) as raised:
        pin_rpm_builder.main(["--pin", str(pin), "--digest", DIGEST])
    assert raised.value.code == 2


def test_the_workflow_names_the_same_image() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/rpm-builder-image.yml").read_text()
    )
    assert workflow["env"]["BUILDER_IMAGE"] == pin_rpm_builder.IMAGE
