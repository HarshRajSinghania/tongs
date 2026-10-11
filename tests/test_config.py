"""Tests for configuration loader."""

import logging
from datetime import timedelta
from pathlib import Path

import pytest

from tongs.config import (
    Config,
    HostConfig,
    app_config,
    config_dir,
    load_config,
    parse_retention,
)
from tongs.errors import ConfigError, ConfigFileError


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
syntax_theme = "friendly"
diff_style = "split"
show_draft_mrs = false
ascii_mode = true
color_scheme = "dark"

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
        assert cfg.syntax_theme == "friendly"
        assert cfg.diff_style == "split"
        assert cfg.show_draft_mrs is False
        assert cfg.ascii_mode is True
        assert cfg.color_scheme == "dark"
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
        assert cfg.theme == "textual-dark"
        assert cfg.syntax_theme == "monokai"
        assert cfg.diff_style == "unified"
        assert cfg.show_draft_mrs is True
        assert cfg.color_scheme == "system"
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


class TestUnknownNames:
    def test_warnings_replace_control_characters_in_names(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        toml_file = _write(
            tmp_path,
            '"x\\u001b[31m" = 1\n'
            '["s\\u001b"]\na = 1\n'
            '[ui]\n"k\\u0007" = 1\n'
            '[hosts."h\\u001b"]\nhostname = "git.example.com"\n'
            'forge_type = "gitlab"\n"z\\u001b" = 1\n',
        )
        with caplog.at_level(logging.WARNING, logger="tongs.config"):
            load_config(toml_file)
        warnings = _config_warnings(caplog)
        assert len(warnings) == 4
        assert not any(c in w for w in warnings for c in "\x1b\x07")
        assert any("x?[31m" in w for w in warnings)


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


def _write(tmp_path: Path, body: str) -> Path:
    toml_file = tmp_path / "config.toml"
    toml_file.write_text(body, encoding="utf-8")
    return toml_file


def _config_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "tongs.config"]


class TestSyntaxErrors:
    def test_unquoted_value_names_the_file_line_and_column(
        self, tmp_path: Path
    ) -> None:
        toml_file = _write(tmp_path, "[general]\nscan_root = ~/git\n")
        with pytest.raises(ConfigError) as caught:
            load_config(toml_file)
        message = str(caught.value)
        assert str(toml_file) in message
        assert "line 2, column 13" in message
        assert "\n" not in message
        assert isinstance(caught.value, ConfigFileError)
        assert caught.value.path == toml_file

    def test_duplicate_key_is_a_syntax_error(self, tmp_path: Path) -> None:
        toml_file = _write(tmp_path, "[cache]\ndiff_ttl = 1\ndiff_ttl = 2\n")
        with pytest.raises(ConfigFileError, match="line 3"):
            load_config(toml_file)

    def test_invalid_utf8_is_reported(self, tmp_path: Path) -> None:
        toml_file = tmp_path / "config.toml"
        toml_file.write_bytes(b'[general]\nscan_root = "\xff"\n')
        with pytest.raises(ConfigFileError, match="UTF-8"):
            load_config(toml_file)

    def test_unreadable_path_is_reported(self, tmp_path: Path) -> None:
        directory = tmp_path / "config.toml"
        directory.mkdir()
        with pytest.raises(ConfigFileError, match="cannot be read"):
            load_config(directory)

    def test_message_never_repeats_the_value(self, tmp_path: Path) -> None:
        toml_file = _write(tmp_path, '[cache]\nmr_list_ttl = "token-zqSECRETzq"\n')
        with pytest.raises(ConfigFileError) as caught:
            load_config(toml_file)
        assert "SECRET" not in str(caught.value)


class TestTypeErrors:
    @pytest.mark.parametrize(
        ("body", "named"),
        [
            ('[cache]\nmr_list_ttl = "60"\n', "[cache] mr_list_ttl"),
            ("[cache]\nmr_list_ttl = true\n", "[cache] mr_list_ttl"),
            ("[cache]\ndiff_ttl = 1.5\n", "[cache] diff_ttl"),
            ("[cache]\nmax_size_mb = 0\n", "[cache] max_size_mb"),
            ("[cache]\ndiff_ttl = -1\n", "[cache] diff_ttl"),
            ("[general]\nscan_root = 5\n", "[general] scan_root"),
            ('[general]\nscan_root = ""\n', "[general] scan_root"),
            ('[general]\nscan_depth = "5"\n', "[general] scan_depth"),
            ("[general]\nscan_depth = -1\n", "[general] scan_depth"),
            ("[editor]\ncommand = []\n", "[editor] command"),
            (
                '[editor]\nexternal_editor_enabled = "no"\n',
                "[editor] external_editor_enabled",
            ),
            ("[ui]\nascii_mode = 1\n", "[ui] ascii_mode"),
            ("[ui]\nshow_draft_mrs = 0\n", "[ui] show_draft_mrs"),
            ("[ui]\ntheme = 3\n", "[ui] theme"),
            ("[ui]\ndiff_style = false\n", "[ui] diff_style"),
            ("[ui]\nsyntax_theme = 1\n", "[ui] syntax_theme"),
            ("[ui]\ncolor_scheme = true\n", "[ui] color_scheme"),
            ("[ui]\ncolor_scheme = 1\n", "[ui] color_scheme"),
            ("[concurrency]\nmax_parallel = 0\n", "[concurrency] max_parallel"),
            (
                '[concurrency]\nrequest_timeout = "30"\n',
                "[concurrency] request_timeout",
            ),
            ("[concurrency]\nrequest_timeout = 0\n", "[concurrency] request_timeout"),
            ('hosts = ["x"]\n', "[hosts]"),
            ('general = "x"\n', "[general]"),
            ('drafts = "24h"\n', "[drafts]"),
            ("plugins = 1\n", "[plugins]"),
            ('[hosts]\nwork = "gitlab.example.com"\n', "[hosts.work]"),
            ('[plugins]\nfleet = "on"\n', "[plugins.fleet]"),
            ('[plugins.fleet]\nenabled = "false"\n', "[plugins.fleet] enabled"),
        ],
    )
    def test_rejected_at_load_with_the_key_named(
        self, tmp_path: Path, body: str, named: str
    ) -> None:
        toml_file = _write(tmp_path, body)
        with pytest.raises(ConfigError) as caught:
            load_config(toml_file)
        message = str(caught.value)
        assert named in message
        assert str(toml_file) in message

    def test_zero_ttls_and_depth_are_accepted(self, tmp_path: Path) -> None:
        toml_file = _write(
            tmp_path,
            "[general]\nscan_depth = 0\n[cache]\nmr_list_ttl = 0\ndiff_ttl = 0\n",
        )
        cfg = load_config(toml_file)
        assert (cfg.scan_depth, cfg.mr_list_ttl, cfg.diff_ttl) == (0, 0, 0)

    def test_plugin_tables_pass_through(self, tmp_path: Path) -> None:
        toml_file = _write(
            tmp_path,
            "[plugins.fleet]\nenabled = false\nmonitor_interval = 30\n",
        )
        assert load_config(toml_file).plugin_config == {
            "fleet": {"enabled": False, "monitor_interval": 30}
        }


class TestHosts:
    @pytest.mark.parametrize(
        ("hostname", "problem"),
        [
            ('"git.example.com:8443"', "without a port"),
            ('"[::1]"', "not an IPv6 address"),
            ('"https://git.example.com"', "without a scheme"),
            ('"git.example.com/group"', "without a path"),
            ('"Git.Example.com"', "lowercase"),
            ('""', "must not be empty"),
            ('"git example.com"', "host name"),
            ('"-git.example.com"', "host name"),
            ('"git..example.com"', "host name"),
            ("42", "quoted string"),
        ],
    )
    def test_invalid_hostname_is_rejected(
        self, tmp_path: Path, hostname: str, problem: str
    ) -> None:
        toml_file = _write(
            tmp_path,
            f'[hosts.work]\nhostname = {hostname}\nforge_type = "gitlab"\n',
        )
        with pytest.raises(ConfigError) as caught:
            load_config(toml_file)
        assert "[hosts.work] hostname" in str(caught.value)
        assert problem in str(caught.value)

    def test_control_characters_in_a_table_name_keep_one_line(
        self, tmp_path: Path
    ) -> None:
        toml_file = _write(tmp_path, '[hosts."a\\nb"]\nforge_type = "gitlab"\n')
        with pytest.raises(ConfigError) as caught:
            load_config(toml_file)
        assert "\n" not in str(caught.value)
        assert "[hosts.a?b] hostname is required" in str(caught.value)

    def test_hostname_is_required(self, tmp_path: Path) -> None:
        toml_file = _write(tmp_path, '[hosts.work]\nforge_type = "gitlab"\n')
        with pytest.raises(ConfigError, match=r"\[hosts\.work\] hostname is required"):
            load_config(toml_file)

    @pytest.mark.parametrize("forge_type", ['"gitea"', '""', '"GitLab"', "1"])
    def test_forge_type_must_be_github_or_gitlab(
        self, tmp_path: Path, forge_type: str
    ) -> None:
        toml_file = _write(
            tmp_path,
            f'[hosts.work]\nhostname = "git.example.com"\nforge_type = {forge_type}\n',
        )
        with pytest.raises(ConfigError, match=r"\[hosts\.work\] forge_type"):
            load_config(toml_file)

    def test_forge_type_is_required(self, tmp_path: Path) -> None:
        toml_file = _write(tmp_path, '[hosts.work]\nhostname = "git.example.com"\n')
        with pytest.raises(ConfigError, match="forge_type is required"):
            load_config(toml_file)

    @pytest.mark.parametrize(
        "hostname", ["git.example.com", "localhost", "10.0.0.5", "git_lab.corp"]
    )
    def test_valid_hostnames_load(self, tmp_path: Path, hostname: str) -> None:
        toml_file = _write(
            tmp_path,
            f'[hosts.work]\nhostname = "{hostname}"\nforge_type = "github"\n',
        )
        cfg = load_config(toml_file)
        assert cfg.extra_github_hosts == frozenset({hostname})


class TestUnknownKeys:
    def test_each_unknown_key_and_section_warns_once_and_loading_continues(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        toml_file = _write(
            tmp_path,
            """\
stray = 1

[general]
scan_root = "/work"
scan_dpth = 3

[ui]
colour = "dark"

[extras]
anything = true

[hosts.work]
hostname = "git.example.com"
forge_type = "gitlab"
token = "zqSECRETzq"
""",
        )
        with caplog.at_level(logging.WARNING, logger="tongs.config"):
            cfg = load_config(toml_file)
        assert cfg.scan_root == "/work"
        assert cfg.scan_depth == 5
        assert cfg.extra_gitlab_hosts == frozenset({"git.example.com"})
        warnings = _config_warnings(caplog)
        assert len(warnings) == 5
        joined = "\n".join(warnings)
        for named in (
            "stray",
            "[general] scan_dpth",
            "[ui] colour",
            "[extras]",
            "[hosts.work] token",
        ):
            assert named in joined
        assert "SECRET" not in joined

    def test_a_valid_file_logs_nothing(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        toml_file = _write(tmp_path, '[general]\nscan_root = "/work"\n')
        with caplog.at_level(logging.WARNING, logger="tongs.config"):
            load_config(toml_file)
        assert _config_warnings(caplog) == []


class TestUiStyles:
    def test_defaults_keep_todays_look(self) -> None:
        cfg = Config()
        assert cfg.theme == "textual-dark"
        assert cfg.syntax_theme == "monokai"
        assert cfg.diff_style == "unified"

    def test_old_monokai_theme_is_kept_as_the_app_theme(self, tmp_path: Path) -> None:
        cfg = load_config(_write(tmp_path, '[ui]\ntheme = "monokai"\n'))
        assert cfg.theme == "monokai"
        assert cfg.syntax_theme == "monokai"

    @pytest.mark.parametrize("style", ["unified", "split"])
    def test_diff_style_accepts_both_layouts(self, tmp_path: Path, style: str) -> None:
        cfg = load_config(_write(tmp_path, f'[ui]\ndiff_style = "{style}"\n'))
        assert cfg.diff_style == style

    @pytest.mark.parametrize("style", ["side-by-side", "Split", "", "inline"])
    def test_invalid_diff_style_falls_back_with_one_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, style: str
    ) -> None:
        toml_file = _write(tmp_path, f'[ui]\ndiff_style = "{style}"\n')
        with caplog.at_level(logging.WARNING, logger="tongs.config"):
            cfg = load_config(toml_file)
        assert cfg.diff_style == "unified"
        warnings = _config_warnings(caplog)
        assert len(warnings) == 1
        assert "[ui] diff_style" in warnings[0]
        assert '"unified" or "split"' in warnings[0]

    @pytest.mark.parametrize("style", ["friendly", "solarized-light", "default"])
    def test_syntax_theme_accepts_pygments_styles(
        self, tmp_path: Path, style: str
    ) -> None:
        cfg = load_config(_write(tmp_path, f'[ui]\nsyntax_theme = "{style}"\n'))
        assert cfg.syntax_theme == style

    @pytest.mark.parametrize("style", ["textual-dark", "Monokai", "", "no-such"])
    def test_unknown_syntax_theme_falls_back_with_one_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, style: str
    ) -> None:
        toml_file = _write(tmp_path, f'[ui]\nsyntax_theme = "{style}"\n')
        with caplog.at_level(logging.WARNING, logger="tongs.config"):
            cfg = load_config(toml_file)
        assert cfg.syntax_theme == "monokai"
        warnings = _config_warnings(caplog)
        assert len(warnings) == 1
        assert "[ui] syntax_theme" in warnings[0]
        assert "Pygments" in warnings[0]

    def test_app_theme_is_not_checked_by_the_loader(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # The app checks the name against the themes Textual and plugins offer.
        toml_file = _write(tmp_path, '[ui]\ntheme = "no-such-theme"\n')
        with caplog.at_level(logging.WARNING, logger="tongs.config"):
            cfg = load_config(toml_file)
        assert cfg.theme == "no-such-theme"
        assert _config_warnings(caplog) == []


class TestColorScheme:
    def test_default_follows_the_system(self) -> None:
        assert Config().color_scheme == "system"

    @pytest.mark.parametrize("scheme", ["system", "light", "dark"])
    def test_accepts_each_scheme(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, scheme: str
    ) -> None:
        toml_file = _write(tmp_path, f'[ui]\ncolor_scheme = "{scheme}"\n')
        with caplog.at_level(logging.WARNING, logger="tongs.config"):
            cfg = load_config(toml_file)
        assert cfg.color_scheme == scheme
        assert _config_warnings(caplog) == []

    @pytest.mark.parametrize("scheme", ["Dark", "auto", "", "night", " light"])
    def test_unknown_scheme_falls_back_to_system_with_one_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, scheme: str
    ) -> None:
        toml_file = _write(tmp_path, f'[ui]\ncolor_scheme = "{scheme}"\n')
        with caplog.at_level(logging.WARNING, logger="tongs.config"):
            cfg = load_config(toml_file)
        assert cfg.color_scheme == "system"
        warnings = _config_warnings(caplog)
        assert len(warnings) == 1
        assert "[ui] color_scheme" in warnings[0]
        assert '"system", "light" or "dark"' in warnings[0]

    @pytest.mark.parametrize("value", ["true", "1", "[]", "{ scheme = 'dark' }"])
    def test_a_value_that_is_not_a_string_stops_loading(
        self, tmp_path: Path, value: str
    ) -> None:
        toml_file = _write(tmp_path, f"[ui]\ncolor_scheme = {value}\n")
        with pytest.raises(ConfigFileError) as caught:
            load_config(toml_file)
        message = str(caught.value)
        assert "[ui] color_scheme" in message
        assert "quoted string" in message

    def test_does_not_change_the_terminal_theme(self, tmp_path: Path) -> None:
        cfg = load_config(_write(tmp_path, '[ui]\ncolor_scheme = "light"\n'))
        assert cfg.theme == "textual-dark"
        assert cfg.syntax_theme == "monokai"


class TestAppConfig:
    def test_returns_the_apps_config(self) -> None:
        cfg = Config(syntax_theme="friendly")

        class Owner:
            config = cfg

        assert app_config(Owner()) is cfg

    def test_defaults_when_the_app_has_none(self) -> None:
        assert app_config(object()) == Config()

        class Owner:
            config = "not a config"

        assert app_config(Owner()) == Config()
