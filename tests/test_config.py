"""Tests for configuration loader."""

import logging
from datetime import timedelta
from pathlib import Path

import pytest

from tongs.config import Config, HostConfig, config_dir, load_config, parse_retention


class TestLoadConfig:
    def test_nonexistent_path_returns_defaults(self, tmp_path):
        cfg = load_config(tmp_path / "missing.toml")
        assert cfg.scan_root == "~/git"
        assert cfg.scan_depth == 5

    def test_complete_toml(self, tmp_path):
        toml_file = tmp_path / "config.toml"
        toml_file.write_text(
            """\
[general]
scan_root = "/projects"
scan_depth = 3

[editor]
command = "nvim"
external_editor_enabled = false

[ui]
theme = "dracula"
diff_style = "side-by-side"
show_draft_mrs = false
ascii_mode = true

[cache]
mr_list_ttl = 120
diff_ttl = 600
max_size_mb = 200

[concurrency]
max_parallel = 4
request_timeout = 15

[hosts.internal]
hostname = "git.corp.com"
forge_type = "gitlab"

[hosts.gh-enterprise]
hostname = "github.corp.com"
forge_type = "github"

[plugins.jira]
url = "https://jira.corp.com"
"""
        )
        cfg = load_config(toml_file)
        assert cfg.scan_root == "/projects"
        assert cfg.scan_depth == 3
        assert cfg.editor_command == "nvim"
        assert cfg.external_editor_enabled is False
        assert cfg.theme == "dracula"
        assert cfg.diff_style == "side-by-side"
        assert cfg.show_draft_mrs is False
        assert cfg.ascii_mode is True
        assert cfg.mr_list_ttl == 120
        assert cfg.diff_ttl == 600
        assert cfg.max_cache_size_mb == 200
        assert cfg.max_parallel == 4
        assert cfg.request_timeout == 15
        assert "internal" in cfg.extra_hosts
        assert cfg.extra_hosts["internal"].hostname == "git.corp.com"
        assert cfg.extra_hosts["internal"].forge_type == "gitlab"
        assert "gh-enterprise" in cfg.extra_hosts
        assert cfg.extra_hosts["gh-enterprise"].hostname == "github.corp.com"
        assert cfg.extra_hosts["gh-enterprise"].forge_type == "github"
        assert cfg.plugin_config["jira"]["url"] == "https://jira.corp.com"

    def test_partial_toml_falls_back_to_defaults(self, tmp_path):
        toml_file = tmp_path / "config.toml"
        toml_file.write_text(
            """\
[general]
scan_root = "/work"
"""
        )
        cfg = load_config(toml_file)
        assert cfg.scan_root == "/work"
        assert cfg.scan_depth == 5
        assert cfg.editor_command == ""
        assert cfg.theme == "monokai"
        assert cfg.mr_list_ttl == 60
        assert cfg.max_parallel == 8
        assert cfg.extra_hosts == {}
        assert cfg.plugin_config == {}


class TestScanRootPath:
    def test_expands_tilde(self):
        cfg = Config(scan_root="~/projects")
        assert cfg.scan_root_path == Path.home() / "projects"


class TestExtraHosts:
    def test_extra_gitlab_hosts_filters_correctly(self):
        cfg = Config(
            extra_hosts={
                "gl1": HostConfig(hostname="gl.corp.com", forge_type="gitlab"),
                "gh1": HostConfig(hostname="gh.corp.com", forge_type="github"),
                "gl2": HostConfig(hostname="gl2.corp.com", forge_type="gitlab"),
            }
        )
        result = cfg.extra_gitlab_hosts
        assert result == frozenset({"gl.corp.com", "gl2.corp.com"})

    def test_extra_github_hosts_filters_correctly(self):
        cfg = Config(
            extra_hosts={
                "gl1": HostConfig(hostname="gl.corp.com", forge_type="gitlab"),
                "gh1": HostConfig(hostname="gh.corp.com", forge_type="github"),
                "gh2": HostConfig(hostname="gh2.corp.com", forge_type="github"),
            }
        )
        result = cfg.extra_github_hosts
        assert result == frozenset({"gh.corp.com", "gh2.corp.com"})

    def test_untyped_or_misspelled_hosts_are_not_admitted(self):
        cfg = Config(
            extra_hosts={
                "x": HostConfig(hostname="x.corp", forge_type=""),
                "y": HostConfig(hostname="y.corp", forge_type="gitlb"),
            }
        )
        assert cfg.extra_gitlab_hosts == frozenset()
        assert cfg.extra_github_hosts == frozenset()


def test_config_dir_uses_platform_user_config_dir(monkeypatch) -> None:
    monkeypatch.setattr(
        "tongs.config.platformdirs.user_config_dir", lambda name: "/tmp/config/tongs"
    )
    assert config_dir() == Path("/tmp/config/tongs")


class TestSubmittedRetention:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("24h", timedelta(hours=24)),
            ("7d", timedelta(days=7)),
            ("1h", timedelta(hours=1)),
            ("365d", timedelta(days=365)),
            ("8760h", timedelta(days=365)),
        ],
    )
    def test_accepts_hours_and_days(self, value: str, expected: timedelta) -> None:
        assert parse_retention(value) == expected

    @pytest.mark.parametrize(
        "value",
        [
            "0h",
            "0d",
            "24",
            "24H",
            "7D",
            "1w",
            "-1d",
            "366d",
            "8761h",
            "",
            " 24h",
            "24h\n",
            "07d",
            "1.5d",
            "١h",
            24,
            1.5,
            True,
            None,
            ["24h"],
        ],
    )
    def test_rejects_other_values(self, value: object) -> None:
        with pytest.raises(ValueError):
            parse_retention(value)

    def test_default_is_24_hours(self, tmp_path: Path) -> None:
        assert Config().submitted_draft_retention == timedelta(hours=24)
        assert load_config(tmp_path / "missing.toml").submitted_draft_retention == (
            timedelta(hours=24)
        )
        toml_file = tmp_path / "config.toml"
        toml_file.write_text("[general]\nscan_depth = 2\n")
        assert load_config(toml_file).submitted_draft_retention == timedelta(hours=24)

    def test_reads_the_drafts_table(self, tmp_path: Path) -> None:
        toml_file = tmp_path / "config.toml"
        toml_file.write_text('[drafts]\nsubmitted_retention = "7d"\n')
        assert load_config(toml_file).submitted_draft_retention == timedelta(days=7)

    @pytest.mark.parametrize(
        "body",
        [
            '[drafts]\nsubmitted_retention = "1w"\n',
            "[drafts]\nsubmitted_retention = 24\n",
            '[drafts]\nsubmitted_retention = ""\n',
            'drafts = "24h"\n',
        ],
    )
    def test_invalid_value_falls_back_with_one_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, body: str
    ) -> None:
        toml_file = tmp_path / "config.toml"
        toml_file.write_text(body)
        with caplog.at_level(logging.WARNING, logger="tongs.config"):
            cfg = load_config(toml_file)
        assert cfg.submitted_draft_retention == timedelta(hours=24)
        warnings = [r.getMessage() for r in caplog.records if r.name == "tongs.config"]
        assert len(warnings) == 1
        assert "submitted_retention" in warnings[0]
        assert "Xh" in warnings[0] and "Xd" in warnings[0]


class TestFileContentTtl:
    def test_default_is_one_day(self, tmp_path: Path) -> None:
        assert Config().file_content_ttl == timedelta(days=1)
        assert load_config(tmp_path / "missing.toml").file_content_ttl == (
            timedelta(days=1)
        )
        toml_file = tmp_path / "config.toml"
        toml_file.write_text("[cache]\ndiff_ttl = 60\n")
        assert load_config(toml_file).file_content_ttl == timedelta(days=1)

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ('"12h"', timedelta(hours=12)),
            ('"7d"', timedelta(days=7)),
            ('"1h"', timedelta(hours=1)),
            ('"365d"', timedelta(days=365)),
        ],
    )
    def test_reads_the_cache_table(
        self, tmp_path: Path, value: str, expected: timedelta
    ) -> None:
        toml_file = tmp_path / "config.toml"
        toml_file.write_text(f"[cache]\nfile_content_ttl = {value}\n")
        assert load_config(toml_file).file_content_ttl == expected

    @pytest.mark.parametrize(
        "value",
        ['"0h"', "24", '"24H"', '"1w"', '"-1d"', '"366d"', '""', "1.5", "true"],
    )
    def test_invalid_value_falls_back_with_one_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, value: str
    ) -> None:
        toml_file = tmp_path / "config.toml"
        toml_file.write_text(f"[cache]\nfile_content_ttl = {value}\n")
        with caplog.at_level(logging.WARNING, logger="tongs.config"):
            cfg = load_config(toml_file)
        assert cfg.file_content_ttl == timedelta(days=1)
        warnings = [r.getMessage() for r in caplog.records if r.name == "tongs.config"]
        assert len(warnings) == 1
        assert "file_content_ttl" in warnings[0]
        assert "Xh" in warnings[0] and "Xd" in warnings[0]
