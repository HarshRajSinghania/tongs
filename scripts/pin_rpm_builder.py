#!/usr/bin/env python3
"""Validate and rewrite the RPM builder image pin.

``packaging/rpm/builder/image.json`` records which published builder image the
RPM harness may use: the image, its digest, the tag it was pushed under, the
Fedora 44 base digest it was built from, the source commit and the workflow
run that built it.  Until the first image is published every field but
``image`` is ``null``, a state ``--check`` accepts and ``--require-published``
refuses, so nothing can pull an image that does not exist.

With ``--check`` the program validates the pin and exits.  Otherwise it writes
the pin from the given values in canonical form, unless the pin already holds
that digest, and prints ``changed=true`` or ``changed=false`` for
``$GITHUB_OUTPUT``.  The builder workflow's pin job runs it on the runner's
Python, so it is standard library only.
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PIN_PATH = ROOT / "packaging/rpm/builder/image.json"
IMAGE = "ghcr.io/andre-motta/tongs-rpm-builder"
REPOSITORY_URL = "https://github.com/andre-motta/tongs"
FIELDS = (
    "image",
    "digest",
    "tag",
    "base_digest",
    "source_commit",
    "run_id",
    "run_url",
)
#: Every field the workflow fills; ``image`` is fixed.
PUBLISHED_FIELDS = FIELDS[1:]

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_TAG = re.compile(r"fedora44-(?P<date>[0-9]{8})-(?P<short>[0-9a-f]{12})")


class PinError(ValueError):
    """The pin, or a value meant for it, breaks the contract."""


def _require_digest(name: str, value: object) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise PinError(f"{name} must be sha256: followed by 64 lowercase hex digits")
    return value


def validate(pin: object) -> dict[str, Any]:
    """Return ``pin`` when it is a valid published or unpublished pin."""

    if not isinstance(pin, dict):
        raise PinError("the pin must be a JSON object")
    if set(pin) != set(FIELDS):
        raise PinError(f"the pin must hold exactly the fields {sorted(FIELDS)}")
    if pin["image"] != IMAGE:
        raise PinError(f"image must be exactly {IMAGE}")
    if all(pin[name] is None for name in PUBLISHED_FIELDS):
        return pin
    missing = [name for name in PUBLISHED_FIELDS if pin[name] is None]
    if missing:
        raise PinError(f"a published pin must set {missing}")
    _require_digest("digest", pin["digest"])
    _require_digest("base_digest", pin["base_digest"])
    commit = pin["source_commit"]
    if not isinstance(commit, str) or not _COMMIT.fullmatch(commit):
        raise PinError("source_commit must be a full 40-character commit SHA")
    tag = pin["tag"]
    match = _TAG.fullmatch(tag) if isinstance(tag, str) else None
    if match is None:
        raise PinError("tag must be fedora44-<YYYYMMDD>-<12 hex characters>")
    try:
        datetime.datetime.strptime(match.group("date"), "%Y%m%d")
    except ValueError as error:
        raise PinError("tag must carry a real calendar date") from error
    if not commit.startswith(match.group("short")):
        raise PinError("tag must end with the source commit's first 12 characters")
    run_id = pin["run_id"]
    if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id <= 0:
        raise PinError("run_id must be a positive integer")
    if pin["run_url"] != f"{REPOSITORY_URL}/actions/runs/{run_id}":
        raise PinError(f"run_url must be {REPOSITORY_URL}/actions/runs/<run_id>")
    return pin


def is_published(pin: dict[str, Any]) -> bool:
    return pin["digest"] is not None


def render(pin: dict[str, Any]) -> str:
    """Return the canonical text of a valid pin."""

    validate(pin)
    return json.dumps({name: pin[name] for name in FIELDS}, indent=2) + "\n"


def load(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise PinError(f"cannot read {path}: {error}") from error
    pin = validate(document)
    if path.read_text() != render(pin):
        raise PinError(f"{path} is not in canonical form")
    return pin


def check(path: Path, *, require_published: bool) -> None:
    pin = load(path)
    if require_published and not is_published(pin):
        raise PinError(
            "the builder image is not published yet; image.json has no digest"
        )


def write(
    path: Path,
    *,
    digest: str,
    tag: str,
    base_digest: str,
    source_commit: str,
    run_id: int,
) -> bool:
    """Write the new pin and return whether the file changed."""

    current = load(path)
    pin = validate(
        {
            "image": IMAGE,
            "digest": digest,
            "tag": tag,
            "base_digest": base_digest,
            "source_commit": source_commit,
            "run_id": run_id,
            "run_url": f"{REPOSITORY_URL}/actions/runs/{run_id}",
        }
    )
    if current["digest"] == pin["digest"]:
        return False
    path.write_text(render(pin))
    return True


def _run_id(value: str) -> int:
    if not value.isdigit() or int(value) <= 0:
        raise argparse.ArgumentTypeError("run id must be a positive integer")
    return int(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--pin", type=Path, default=PIN_PATH, help="pin file path")
    parser.add_argument(
        "--check", action="store_true", help="validate the pin and change nothing"
    )
    parser.add_argument(
        "--require-published",
        action="store_true",
        help="with --check, also refuse a pin that has no published digest",
    )
    parser.add_argument("--digest", help="pushed image digest, sha256:<64 hex>")
    parser.add_argument("--tag", help="pushed tag, fedora44-<YYYYMMDD>-<short sha>")
    parser.add_argument("--base-digest", help="Fedora 44 base image digest")
    parser.add_argument(
        "--source-commit", help="full commit SHA the image was built from"
    )
    parser.add_argument("--run-id", type=_run_id, help="workflow run id that built it")
    arguments = parser.parse_args(argv)
    values = {
        "digest": arguments.digest,
        "tag": arguments.tag,
        "base_digest": arguments.base_digest,
        "source_commit": arguments.source_commit,
        "run_id": arguments.run_id,
    }
    try:
        if arguments.check:
            if any(value is not None for value in values.values()):
                parser.error("--check takes no pin values")
            check(arguments.pin, require_published=arguments.require_published)
            return 0
        if arguments.require_published:
            parser.error("--require-published needs --check")
        missing = [name for name, value in values.items() if value is None]
        if missing:
            parser.error(f"writing a pin needs {', '.join(missing)}")
        changed = write(arguments.pin, **values)
    except PinError as error:
        print(f"pin_rpm_builder: {error}", file=sys.stderr)
        return 1
    print(f"changed={'true' if changed else 'false'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
