"""Release discovery and strict attestation semantics tests."""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from sigstore.models import Bundle
from sigstore.verify.policy import VerificationPolicy

from tongs.desktop.artifact_contract import (
    DesktopPlatform,
    TargetArchitecture,
    TargetOperatingSystem,
)
from tongs.desktop.installer.extract import stage_desktop_release
from tongs.desktop.installer.metadata import (
    INTOTO_PAYLOAD_TYPE,
    RELEASE_MANIFEST_NAME,
    _decode_json,
    discover_release,
    normalize_platform_identity,
    resolve_tag_commit,
    verify_release_metadata,
)
from tongs.desktop.installer.models import (
    AcceptedReleaseState,
    InstallerError,
    InstallerErrorCode,
    InstallerLimits,
    InstallRequest,
)

from .helpers import ARCHIVE_NAME, NOW, SOURCE_COMMIT, documents, release, statement


class MockSemanticVerifier:
    """Explicitly noncryptographic harness for post-verification parsing tests."""

    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.calls: list[tuple[Bundle, VerificationPolicy]] = []

    def verify_dsse(
        self, bundle: Bundle, policy: VerificationPolicy
    ) -> tuple[str, bytes]:
        self.calls.append((bundle, policy))
        return INTOTO_PAYLOAD_TYPE, self.payload


def _release_json(*, immutable: bool = True) -> dict:
    record = release()
    return {
        "id": record.release_id,
        "tag_name": record.tag,
        "draft": False,
        "prerelease": False,
        "immutable": immutable,
        "published_at": NOW.isoformat(),
        "assets": [
            {
                "id": item.asset_id,
                "name": item.name,
                "size": item.byte_count,
                "digest": f"sha256:{item.sha256}",
                "state": "uploaded",
                "url": item.api_url,
            }
            for item in record.assets
        ],
    }


TAG_PATH = "/repos/andre-motta/tongs/releases/tags/"
LIST_PATH = "/repos/andre-motta/tongs/releases"


def _realistic_release(index: int) -> dict:
    """An unrelated older release about 32 KiB long, as GitHub returns it."""
    value = _release_json()
    value.update(
        {
            "id": 10_000 + index,
            "tag_name": f"v0.{index}.0",
            "body": "Release notes. " * 2_000,
        }
    )
    return value


class TagServer:
    """Serve releases by tag and record every request; listing is a failure."""

    def __init__(self, releases: list[dict]) -> None:
        self.by_tag = {item["tag_name"]: item for item in releases}
        self.paths: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        if request.url.path == LIST_PATH:
            raise AssertionError("a known version must not enumerate releases")
        assert request.url.path.startswith(TAG_PATH)
        found = self.by_tag.get(request.url.path.removeprefix(TAG_PATH))
        if found is None:
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(200, json=found)


@pytest.mark.asyncio
async def test_discovers_only_exact_immutable_desktop_release() -> None:
    pages: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        pages.append(request.url.params["page"])
        return httpx.Response(
            200,
            json=[
                {"tag_name": "unrelated-v99.0.0"},
                _release_json(),
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        selected = await discover_release(
            client,
            InstallRequest(),
            limits=InstallerLimits(releases_per_page=100),
            clock=lambda: NOW,
        )

    assert selected.version == "1.2.3"
    assert pages == ["1"]


@pytest.mark.asyncio
async def test_newer_prerelease_and_draft_do_not_hide_latest_stable_release() -> None:
    prerelease = _release_json()
    prerelease.update(
        {
            "id": 9001,
            "tag_name": "v9.0.0",
            "prerelease": True,
        }
    )
    draft = _release_json()
    draft.update(
        {
            "id": 9002,
            "tag_name": "v8.0.0",
            "draft": True,
        }
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200, json=[prerelease, draft, _release_json()]
            )
        )
    ) as client:
        selected = await discover_release(
            client,
            InstallRequest(),
            limits=InstallerLimits(releases_per_page=100),
            clock=lambda: NOW,
        )

    assert selected.version == "1.2.3"


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", ["requested", "core", "newest"])
async def test_mutable_desktop_release_is_rejected(selection: str) -> None:
    mutable = _release_json(immutable=False)
    older = _release_json()
    older.update({"id": 9004, "tag_name": "v1.0.0"})
    tags = TagServer([mutable])

    def handler(request: httpx.Request) -> httpx.Response:
        if selection == "newest":
            return httpx.Response(200, json=[mutable, older])
        return tags(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(InstallerError) as raised:
            await discover_release(
                client,
                InstallRequest("1.2.3" if selection == "requested" else None),
                limits=InstallerLimits(),
                clock=lambda: NOW,
                core_version="1.2.3" if selection == "core" else None,
            )
    assert raised.value.code is InstallerErrorCode.INVALID_METADATA
    assert str(raised.value) == "Desktop release metadata is not immutable."
    if selection != "newest":
        assert tags.paths == [f"{TAG_PATH}v1.2.3"]


@pytest.mark.asyncio
async def test_release_enumeration_fails_closed_at_page_bound() -> None:
    pages: list[str] = []

    def response(request: httpx.Request) -> httpx.Response:
        assert request.url.path == LIST_PATH
        pages.append(request.url.params["page"])
        return httpx.Response(200, json=[{"tag_name": "unrelated-v1.0.0"}])

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        with pytest.raises(InstallerError) as raised:
            await discover_release(
                client,
                InstallRequest(),
                limits=InstallerLimits(max_release_pages=2, releases_per_page=1),
                clock=lambda: NOW,
            )

    assert raised.value.code is InstallerErrorCode.LIMIT_EXCEEDED
    assert pages == ["1", "2"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("request_version", "core_version"), [("1.2.3", None), (None, "1.2.3")]
)
async def test_known_version_makes_one_tag_request_among_many_releases(
    request_version: str | None, core_version: str | None
) -> None:
    unrelated = [_realistic_release(index) for index in range(40)]
    assert sum(len(json.dumps(item)) for item in unrelated) > 1024 * 1024
    tags = TagServer([*unrelated, _release_json()])
    async with httpx.AsyncClient(transport=httpx.MockTransport(tags)) as client:
        selected = await discover_release(
            client,
            InstallRequest(request_version),
            limits=InstallerLimits(max_release_pages=1, releases_per_page=1),
            clock=lambda: NOW,
            core_version=core_version,
        )

    assert selected.tag == "v1.2.3"
    assert tags.paths == [f"{TAG_PATH}v1.2.3"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "malformation",
    ["not_an_object", "mutable", "missing_assets", "bad_time", "short_tag"],
)
async def test_enumeration_skips_a_malformed_unrelated_release(
    malformation: str,
) -> None:
    broken: object = _release_json()
    assert isinstance(broken, dict)
    broken.update({"id": 9005, "tag_name": "v1.0.0"})
    if malformation == "not_an_object":
        broken = "v1.0.0"
    elif malformation == "mutable":
        broken["immutable"] = False
    elif malformation == "missing_assets":
        broken["assets"] = []
    elif malformation == "bad_time":
        broken["published_at"] = "yesterday"
    else:
        broken["tag_name"] = "v1.0"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json=[broken, _release_json()])
        )
    ) as client:
        selected = await discover_release(
            client,
            InstallRequest(),
            limits=InstallerLimits(releases_per_page=100),
            clock=lambda: NOW,
        )

    assert selected.version == "1.2.3"


@pytest.mark.asyncio
async def test_enumeration_still_fails_closed_for_the_newest_release() -> None:
    newest = _release_json()
    newest.update({"id": 9006, "tag_name": "v9.0.0", "assets": []})
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json=[newest, _release_json()])
        )
    ) as client:
        with pytest.raises(InstallerError) as raised:
            await discover_release(
                client,
                InstallRequest(),
                limits=InstallerLimits(releases_per_page=100),
                clock=lambda: NOW,
            )

    assert raised.value.code is InstallerErrorCode.INVALID_METADATA
    assert str(raised.value) == "Desktop release assets are missing."


@pytest.mark.asyncio
async def test_enumeration_rejects_two_releases_with_the_newest_version() -> None:
    duplicate = _release_json()
    duplicate["id"] = 9007
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json=[duplicate, _release_json()])
        )
    ) as client:
        with pytest.raises(InstallerError, match="ambiguous"):
            await discover_release(
                client,
                InstallRequest(),
                limits=InstallerLimits(releases_per_page=100),
                clock=lambda: NOW,
            )


@pytest.mark.asyncio
async def test_tag_lookup_rejects_a_release_for_another_tag() -> None:
    other = _release_json()
    other["tag_name"] = "v1.2.4"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=other)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(InstallerError) as raised:
            await discover_release(
                client,
                InstallRequest("1.2.3"),
                limits=InstallerLimits(),
                clock=lambda: NOW,
            )

    assert raised.value.code is InstallerErrorCode.INVALID_METADATA
    assert "mismatched" in str(raised.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("draft", [True, False])
async def test_requested_release_that_is_missing_or_unpublished_is_unavailable(
    draft: bool,
) -> None:
    unpublished = _release_json()
    unpublished.update({"draft": True, "published_at": None})
    tags = TagServer([unpublished] if draft else [])
    async with httpx.AsyncClient(transport=httpx.MockTransport(tags)) as client:
        with pytest.raises(InstallerError) as raised:
            await discover_release(
                client,
                InstallRequest("1.2.3"),
                limits=InstallerLimits(),
                clock=lambda: NOW,
                core_version="9.9.9",
            )

    assert raised.value.code is InstallerErrorCode.INVALID_METADATA
    assert str(raised.value) == "The requested desktop release is unavailable."
    assert tags.paths == [f"{TAG_PATH}v1.2.3"]


@pytest.mark.asyncio
async def test_tag_lookup_keeps_the_five_minute_clock_bound() -> None:
    tags = TagServer([_release_json()])
    async with httpx.AsyncClient(transport=httpx.MockTransport(tags)) as client:
        accepted = await discover_release(
            client,
            InstallRequest(),
            limits=InstallerLimits(),
            clock=lambda: NOW - timedelta(minutes=5),
            core_version="1.2.3",
        )
        with pytest.raises(InstallerError) as raised:
            await discover_release(
                client,
                InstallRequest(),
                limits=InstallerLimits(),
                clock=lambda: NOW - timedelta(minutes=5, seconds=1),
                core_version="1.2.3",
            )

    assert accepted.tag == "v1.2.3"
    assert raised.value.code is InstallerErrorCode.INVALID_METADATA
    assert "this computer's clock" in str(raised.value)
    assert "system date and time" in str(raised.value)


@pytest.mark.asyncio
async def test_annotated_release_tag_resolves_to_exact_commit() -> None:
    tag_object = "a" * 40

    def handler(request: httpx.Request) -> httpx.Response:
        if "/git/ref/tags/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "ref": "refs/tags/v1.2.3",
                    "object": {"type": "tag", "sha": tag_object},
                },
            )
        assert request.url.path.endswith(f"/git/tags/{tag_object}")
        return httpx.Response(
            200,
            json={"object": {"type": "commit", "sha": SOURCE_COMMIT}},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        resolved = await resolve_tag_commit(client, "v1.2.3", limits=InstallerLimits())

    assert resolved == SOURCE_COMMIT


def test_metadata_json_rejects_duplicate_keys_nonfinite_and_invalid_unicode() -> None:
    for document in (
        b'{"id":1,"id":2}',
        b'{"value":NaN}',
        b'{"value":"\\ud800"}',
        b'{"value":' + b"9" * 5_000 + b"}",
    ):
        with pytest.raises(InstallerError):
            _decode_json(document, 10_000, "test document")


@pytest.mark.asyncio
async def test_mocked_verifier_exercises_exact_production_semantics() -> None:
    manifest, archive, bundle = documents()
    subjects = {
        RELEASE_MANIFEST_NAME: hashlib.sha256(manifest).hexdigest(),
        ARCHIVE_NAME: hashlib.sha256(archive).hexdigest(),
    }
    verifier = MockSemanticVerifier(statement(subjects))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/assets/1"):
            return httpx.Response(200, content=manifest)
        if request.url.path.endswith("/assets/2"):
            return httpx.Response(200, content=bundle)
        if "/git/ref/tags/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "ref": "refs/tags/v1.2.3",
                    "object": {"type": "commit", "sha": SOURCE_COMMIT},
                },
            )
        raise AssertionError(f"unexpected request {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await verify_release_metadata(
            client,
            verifier,
            release(),
            normalize_platform_identity("linux", "amd64", "fedora", "44", "gnu"),
            core_version="1.2.3",
            rpc_api_major=1,
            plugin_api_major=1,
            limits=InstallerLimits(),
        )

    assert result.source_commit == SOURCE_COMMIT
    assert result.artifact.name == ARCHIVE_NAME
    assert len(verifier.calls) == 1


@pytest.mark.asyncio
async def test_valid_signature_semantics_for_wrong_subject_are_rejected() -> None:
    manifest, _archive, bundle = documents()
    verifier = MockSemanticVerifier(
        statement(
            {
                RELEASE_MANIFEST_NAME: hashlib.sha256(manifest).hexdigest(),
                ARCHIVE_NAME: "0" * 64,
            }
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/assets/1"):
            return httpx.Response(200, content=manifest)
        if request.url.path.endswith("/assets/2"):
            return httpx.Response(200, content=bundle)
        return httpx.Response(
            200,
            json={
                "ref": "refs/tags/v1.2.3",
                "object": {"type": "commit", "sha": SOURCE_COMMIT},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(InstallerError) as raised:
            await verify_release_metadata(
                client,
                verifier,
                release(),
                normalize_platform_identity("linux", "x86_64", "fedora", "44", "gnu"),
                core_version="1.2.3",
                rpc_api_major=1,
                plugin_api_major=1,
                limits=InstallerLimits(),
            )
    assert raised.value.code is InstallerErrorCode.PROVENANCE_FAILED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "malformation",
    [
        "extra_dependency",
        "invalid_invocation",
        "unprefixed_invocation",
        "wrong_repository_id",
        "wrong_repository_owner_id",
    ],
)
async def test_valid_signature_with_noncanonical_github_predicate_is_rejected(
    malformation: str,
) -> None:
    manifest, archive, bundle = documents()
    payload = json.loads(
        statement(
            {
                RELEASE_MANIFEST_NAME: hashlib.sha256(manifest).hexdigest(),
                ARCHIVE_NAME: hashlib.sha256(archive).hexdigest(),
            }
        )
    )
    if malformation == "extra_dependency":
        payload["predicate"]["buildDefinition"]["resolvedDependencies"].append(
            {
                "uri": "git+https://example.test/extra",
                "digest": {"gitCommit": "0" * 40},
            }
        )
    elif malformation == "invalid_invocation":
        payload["predicate"]["runDetails"]["metadata"]["invocationId"] = (
            "https://example.test/actions/runs/1/attempts/1"
        )
    elif malformation == "unprefixed_invocation":
        # Only the official run URL prefix check rejects a bare run path.
        payload["predicate"]["runDetails"]["metadata"]["invocationId"] = (
            "123/attempts/1"
        )
    elif malformation == "wrong_repository_id":
        payload["predicate"]["buildDefinition"]["internalParameters"]["github"][
            "repository_id"
        ] = "1305350435"
    else:
        payload["predicate"]["buildDefinition"]["internalParameters"]["github"][
            "repository_owner_id"
        ] = "30708956"
    verifier = MockSemanticVerifier(json.dumps(payload).encode())

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/assets/1"):
            return httpx.Response(200, content=manifest)
        if request.url.path.endswith("/assets/2"):
            return httpx.Response(200, content=bundle)
        return httpx.Response(
            200,
            json={
                "ref": "refs/tags/v1.2.3",
                "object": {"type": "commit", "sha": SOURCE_COMMIT},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(InstallerError) as raised:
            await verify_release_metadata(
                client,
                verifier,
                release(),
                normalize_platform_identity("linux", "amd64", "fedora", "44", "gnu"),
                core_version="1.2.3",
                rpc_api_major=1,
                plugin_api_major=1,
                limits=InstallerLimits(),
            )

    assert raised.value.code is InstallerErrorCode.PROVENANCE_FAILED


@pytest.mark.asyncio
async def test_unsupported_target_fails_before_any_download() -> None:
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(500)

    unsupported = DesktopPlatform(
        TargetOperatingSystem.LINUX,
        TargetArchitecture.AARCH64,
        "fedora",
        "44",
        "gnu",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(InstallerError) as raised:
            await verify_release_metadata(
                client,
                MockSemanticVerifier(b"{}"),
                release(),
                unsupported,
                core_version="1.2.3",
                rpc_api_major=1,
                plugin_api_major=1,
                limits=InstallerLimits(),
            )
    assert raised.value.code is InstallerErrorCode.UNSUPPORTED_PLATFORM
    assert called is False


@pytest.mark.asyncio
async def test_implicit_selection_takes_the_release_matching_the_running_core() -> None:
    newer = _release_json()
    newer.update({"id": 9003, "tag_name": "v9.0.0"})
    tags = TagServer([newer, _release_json()])
    async with httpx.AsyncClient(transport=httpx.MockTransport(tags)) as client:
        selected = await discover_release(
            client,
            InstallRequest(),
            limits=InstallerLimits(releases_per_page=100),
            clock=lambda: NOW,
            core_version="1.2.3",
        )

    assert selected.version == "1.2.3"
    assert selected.tag == "v1.2.3"
    assert tags.paths == [f"{TAG_PATH}v1.2.3"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("core_version", "tag", "requested"),
    [
        ("1.3.0", "v1.3.0", [f"{TAG_PATH}v1.3.0"]),
        ("1.2.4.dev3+gabcdef0", "v1.2.4", []),
        ("1.2.3+local", "v1.2.3", []),
        ("1.2.3.post1", "v1.2.3", []),
        ("1.2.3rc1", "v1.2.3", []),
    ],
)
async def test_missing_release_for_the_running_core_names_both_versions(
    core_version: str, tag: str, requested: list[str]
) -> None:
    tags = TagServer([_release_json()])
    async with httpx.AsyncClient(transport=httpx.MockTransport(tags)) as client:
        with pytest.raises(InstallerError) as raised:
            await discover_release(
                client,
                InstallRequest(),
                limits=InstallerLimits(releases_per_page=100),
                clock=lambda: NOW,
                core_version=core_version,
            )

    assert raised.value.code is InstallerErrorCode.INVALID_METADATA
    assert f"tongs {core_version}" in str(raised.value)
    assert f"tag {tag};" in str(raised.value)
    assert "retry" in str(raised.value)
    assert tags.paths == requested


@pytest.mark.asyncio
async def test_unpublished_release_for_the_running_core_says_retry_shortly() -> None:
    draft = _release_json()
    draft.update({"draft": True, "published_at": None, "immutable": False})
    tags = TagServer([draft])
    async with httpx.AsyncClient(transport=httpx.MockTransport(tags)) as client:
        with pytest.raises(InstallerError) as raised:
            await discover_release(
                client,
                InstallRequest(),
                limits=InstallerLimits(),
                clock=lambda: NOW,
                core_version="1.2.3",
            )

    assert raised.value.code is InstallerErrorCode.INVALID_METADATA
    assert "No desktop release matches tongs 1.2.3." in str(raised.value)
    assert "retry shortly" in str(raised.value)


@pytest.mark.asyncio
async def test_invalid_core_version_is_rejected_before_any_request() -> None:
    tags = TagServer([])
    async with httpx.AsyncClient(transport=httpx.MockTransport(tags)) as client:
        with pytest.raises(InstallerError, match="core version is invalid"):
            await discover_release(
                client,
                InstallRequest(),
                limits=InstallerLimits(),
                clock=lambda: NOW,
                core_version="not a version",
            )

    assert tags.paths == []


@pytest.mark.asyncio
async def test_rate_limited_tag_lookup_is_retryable() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1788000000"},
            json={"message": "API rate limit exceeded for 203.0.113.9."},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(InstallerError) as raised:
            await discover_release(
                client,
                InstallRequest(),
                limits=InstallerLimits(),
                clock=lambda: NOW,
                core_version="1.2.3",
            )

    assert raised.value.code is InstallerErrorCode.DOWNLOAD_FAILED
    assert raised.value.retryable is True
    assert "rate limit" in str(raised.value)
    assert "203.0.113.9" not in str(raised.value)


class _EmptyReleaseState:
    def __init__(self) -> None:
        self.current: AcceptedReleaseState | None = None

    async def read(self) -> AcceptedReleaseState | None:
        return self.current

    async def compare_and_swap(
        self,
        expected: AcceptedReleaseState | None,
        replacement: AcceptedReleaseState,
    ) -> bool:
        if self.current != expected:
            return False
        self.current = replacement
        return True


@pytest.mark.asyncio
async def test_install_for_the_running_core_fetches_release_metadata_once(
    tmp_path: Path,
) -> None:
    manifest, archive, bundle = documents()
    verifier = MockSemanticVerifier(
        statement(
            {
                RELEASE_MANIFEST_NAME: hashlib.sha256(manifest).hexdigest(),
                ARCHIVE_NAME: hashlib.sha256(archive).hexdigest(),
            }
        )
    )
    tags = TagServer([_realistic_release(index) for index in range(40)])
    tags.by_tag["v1.2.3"] = _release_json()
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        paths.append(path)
        if path.endswith("/releases/assets/1"):
            return httpx.Response(200, content=manifest)
        if path.endswith("/releases/assets/2"):
            return httpx.Response(200, content=bundle)
        if path.endswith("/releases/assets/3"):
            return httpx.Response(200, content=archive)
        if path.endswith("/git/ref/tags/v1.2.3"):
            return httpx.Response(
                200,
                json={
                    "ref": "refs/tags/v1.2.3",
                    "object": {"type": "commit", "sha": SOURCE_COMMIT},
                },
            )
        return tags(request)

    state = _EmptyReleaseState()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        staged = await stage_desktop_release(
            client,
            verifier,
            state,
            lambda: normalize_platform_identity(
                "linux", "x86_64", "fedora", "44", "gnu"
            ),
            tmp_path / "staging",
            core_version="1.2.3",
            rpc_api_major=1,
            plugin_api_major=1,
            limits=InstallerLimits(),
            clock=lambda: NOW,
        )

    release_metadata = [
        path
        for path in paths
        if path.startswith(LIST_PATH) and "/releases/assets/" not in path
    ]
    assert release_metadata == [f"{TAG_PATH}v1.2.3"]
    assert LIST_PATH not in paths
    assert staged.release.tag == "v1.2.3"
    assert state.current is not None and state.current.version == "1.2.3"
    assert staged.launcher_path.is_file()
