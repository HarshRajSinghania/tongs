"""Tests that a bad config.toml reaches the service boundary as a config error."""

from __future__ import annotations

from pathlib import Path

import pytest

from tongs.errors import ConfigError, ConfigFileError
from tongs.services import ApplicationSession, ServiceError
from tongs.services.errors import (
    MAX_CONFIGURATION_MESSAGE,
    ServiceErrorCode,
    translate_error,
)


def test_config_file_error_keeps_its_message() -> None:
    error = ConfigFileError(Path("/home/me/config.toml"), "[cache] diff_ttl is bad.")
    translated = translate_error(error, operation="start_session")
    assert translated.code is ServiceErrorCode.CONFIGURATION_INVALID
    assert translated.message == (
        "Invalid config file /home/me/config.toml: [cache] diff_ttl is bad."
    )
    assert translated.retryable is False
    assert translated.details == (("operation", "start_session"),)


def test_other_config_errors_keep_the_generic_message() -> None:
    translated = translate_error(
        ConfigError("plugin text zqSECRETzq"), operation="start_session"
    )
    assert translated.code is ServiceErrorCode.CONFIGURATION_INVALID
    assert translated.message == "The Tongs configuration is invalid."


def test_config_file_message_is_redacted_and_bounded() -> None:
    token = "glpat-" + "a" * 20
    error = ConfigFileError(Path(f"/srv/{token}/{'x' * 2000}"), "bad.")
    translated = translate_error(error, operation="start_session")
    assert token not in translated.message
    assert "[REDACTED]" in translated.message
    assert len(translated.message) == MAX_CONFIGURATION_MESSAGE


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("[general]\nscan_root = ~/git\n", "line 2, column 13"),
        ('[cache]\nmr_list_ttl = "60"\n', "[cache] mr_list_ttl"),
        ('hosts = ["x"]\n', "[hosts]"),
        (
            '[hosts.work]\nhostname = "git.example.com:8443"\nforge_type = "gitlab"\n',
            "[hosts.work] hostname",
        ),
    ],
)
@pytest.mark.asyncio
async def test_session_start_reports_configuration_invalid(
    tmp_path: Path, body: str, expected: str
) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(body, encoding="utf-8")
    session = ApplicationSession(
        config_path=config_file,
        cache_path=tmp_path / "cache.db",
        draft_db_path=tmp_path / "drafts.db",
    )
    with pytest.raises(ServiceError) as caught:
        await session.start()
    assert caught.value.code is ServiceErrorCode.CONFIGURATION_INVALID
    assert str(config_file) in caught.value.message
    assert expected in caught.value.message
    assert "invalid response" not in caught.value.message
