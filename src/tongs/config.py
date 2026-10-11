"""Configuration loader with platformdirs for cross-platform paths."""

from __future__ import annotations

import datetime as dt
import logging
import re
import tomllib
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Literal

import platformdirs
from pygments.styles import get_all_styles

from tongs.errors import ConfigFileError

APP_NAME = "tongs"

log = logging.getLogger(__name__)

#: How long a submitted review draft stays in local storage by default.
DEFAULT_SUBMITTED_RETENTION = timedelta(hours=24)
#: The shortest and longest accepted ``[drafts] submitted_retention`` values.
MIN_SUBMITTED_RETENTION = timedelta(hours=1)
MAX_SUBMITTED_RETENTION = timedelta(days=365)
#: How long a file body read at a commit stays in the cache database by default.
DEFAULT_FILE_CONTENT_TTL = timedelta(days=1)
_RETENTION_PATTERN = re.compile(r"([1-9][0-9]{0,5})([hd])")
#: The forge types a ``[hosts.<name>]`` table accepts.
FORGE_TYPES = frozenset({"github", "gitlab"})
# One DNS label: letters, digits, hyphens and underscores, not starting or
# ending with a hyphen. Uppercase is rejected separately with its own message.
_HOST_LABEL = r"[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?"
_HOSTNAME_PATTERN = re.compile(rf"{_HOST_LABEL}(?:\.{_HOST_LABEL})*")
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
_TOML_POSITION = re.compile(r"(.*) \(at line (\d+), column (\d+)\)", re.DOTALL)
#: The Textual app theme the terminal uses when ``[ui] theme`` is not set.
DEFAULT_THEME = "textual-dark"
#: The Pygments style diff code uses when ``[ui] syntax_theme`` is not set.
DEFAULT_SYNTAX_THEME = "monokai"
#: The diff layouts ``[ui] diff_style`` accepts; the first is the default.
DIFF_STYLES = ("unified", "split")
#: The desktop colour schemes ``[ui] color_scheme`` accepts; the first, which
#: follows the operating system, is the default.
COLOR_SCHEMES = ("system", "light", "dark")


def config_dir() -> Path:
    return Path(platformdirs.user_config_dir(APP_NAME))


def cache_dir() -> Path:
    return Path(platformdirs.user_cache_dir(APP_NAME))


def data_dir() -> Path:
    return Path(platformdirs.user_data_dir(APP_NAME))


@dataclass
class HostConfig:
    hostname: str
    forge_type: str = ""


@dataclass
class Config:
    scan_root: str = "~/git"
    scan_depth: int = 5
    editor_command: str = ""
    external_editor_enabled: bool = True
    theme: str = DEFAULT_THEME
    syntax_theme: str = DEFAULT_SYNTAX_THEME
    diff_style: str = DIFF_STYLES[0]
    show_draft_mrs: bool = True
    ascii_mode: bool = False
    color_scheme: str = COLOR_SCHEMES[0]
    mr_list_ttl: int = 60
    diff_ttl: int = 300
    max_cache_size_mb: int = 100
    max_parallel: int = 8
    request_timeout: int = 30
    file_content_ttl: timedelta = DEFAULT_FILE_CONTENT_TTL
    submitted_draft_retention: timedelta = DEFAULT_SUBMITTED_RETENTION
    extra_hosts: dict[str, HostConfig] = field(default_factory=dict)
    plugin_config: dict[str, dict] = field(default_factory=dict)

    @property
    def scan_root_path(self) -> Path:
        return Path(self.scan_root).expanduser()

    @property
    def extra_gitlab_hosts(self) -> frozenset[str]:
        return frozenset(
            h.hostname for h in self.extra_hosts.values() if h.forge_type == "gitlab"
        )

    @property
    def extra_github_hosts(self) -> frozenset[str]:
        return frozenset(
            h.hostname for h in self.extra_hosts.values() if h.forge_type == "github"
        )


def parse_retention(value: object) -> timedelta:
    """Parse a ``"<N>h"`` or ``"<N>d"`` retention window.

    ``N`` is a positive integer and the unit is lowercase. The result must lie
    between one hour and 365 days. Anything else raises ``ValueError``.
    """
    match = _RETENTION_PATTERN.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ValueError("retention must be a string such as '24h' or '7d'")
    amount = int(match.group(1))
    window = (
        timedelta(hours=amount) if match.group(2) == "h" else timedelta(days=amount)
    )
    if not MIN_SUBMITTED_RETENTION <= window <= MAX_SUBMITTED_RETENTION:
        raise ValueError("retention must be between 1h and 365d")
    return window


def _submitted_retention(drafts: object) -> timedelta:
    if isinstance(drafts, dict):
        value = drafts.get("submitted_retention")
        if value is None:
            return DEFAULT_SUBMITTED_RETENTION
        try:
            return parse_retention(value)
        except ValueError:
            pass
    log.warning(
        "Ignoring an invalid [drafts] submitted_retention; use Xh for hours or "
        "Xd for days, from 1h to 365d. Using the default of 24h."
    )
    return DEFAULT_SUBMITTED_RETENTION


def _file_content_ttl(cache: object) -> timedelta:
    value = cache.get("file_content_ttl") if isinstance(cache, dict) else None
    if value is None:
        return DEFAULT_FILE_CONTENT_TTL
    try:
        return parse_retention(value)
    except ValueError:
        pass
    log.warning(
        "Ignoring an invalid [cache] file_content_ttl; use Xh for hours or "
        "Xd for days, from 1h to 365d. Using the default of 1d."
    )
    return DEFAULT_FILE_CONTENT_TTL


def _syntax_theme(ui: dict[str, object]) -> str:
    value = ui.get("syntax_theme")
    if value is None:
        return DEFAULT_SYNTAX_THEME
    if value in set(get_all_styles()):
        return str(value)
    log.warning(
        "Ignoring an unknown [ui] syntax_theme; use a Pygments style name such "
        "as monokai or friendly. Using the default of %s.",
        DEFAULT_SYNTAX_THEME,
    )
    return DEFAULT_SYNTAX_THEME


def _diff_style(ui: dict[str, object]) -> str:
    value = ui.get("diff_style")
    if value is None:
        return DIFF_STYLES[0]
    if value in DIFF_STYLES:
        return str(value)
    log.warning(
        'Ignoring an invalid [ui] diff_style; use "unified" or "split". '
        "Using the default of %s.",
        DIFF_STYLES[0],
    )
    return DIFF_STYLES[0]


def _color_scheme(ui: dict[str, object]) -> str:
    value = ui.get("color_scheme")
    if value is None:
        return COLOR_SCHEMES[0]
    if value in COLOR_SCHEMES:
        return str(value)
    log.warning(
        'Ignoring an invalid [ui] color_scheme; use "system", "light" or "dark". '
        "Using the default of %s.",
        COLOR_SCHEMES[0],
    )
    return COLOR_SCHEMES[0]


def app_config(owner: object) -> Config:
    """Return the ``Config`` an app object carries, or the defaults.

    Widgets read settings through this so they also work inside an app that
    has no tongs configuration, such as a test harness.
    """
    config = getattr(owner, "config", None)
    return config if isinstance(config, Config) else Config()


_Kind = Literal["string", "integer", "boolean", "duration"]


@dataclass(frozen=True, slots=True)
class _KeyRule:
    """The type, and for integers the lower bound, one config key accepts."""

    kind: _Kind
    minimum: int | None = None
    non_empty: bool = False


_STRING = _KeyRule("string")
_BOOLEAN = _KeyRule("boolean")
_NON_NEGATIVE = _KeyRule("integer", minimum=0)
_POSITIVE = _KeyRule("integer", minimum=1)
# Durations keep their own warn-and-default handling in the parsers above.
_DURATION = _KeyRule("duration")

#: Every documented key of the fixed sections, and what each accepts.
_SCHEMA: dict[str, dict[str, _KeyRule]] = {
    "general": {
        "scan_root": _KeyRule("string", non_empty=True),
        "scan_depth": _NON_NEGATIVE,
    },
    "editor": {"command": _STRING, "external_editor_enabled": _BOOLEAN},
    "ui": {
        "theme": _STRING,
        "syntax_theme": _STRING,
        "diff_style": _STRING,
        "show_draft_mrs": _BOOLEAN,
        "ascii_mode": _BOOLEAN,
        "color_scheme": _STRING,
    },
    "cache": {
        "mr_list_ttl": _NON_NEGATIVE,
        "diff_ttl": _NON_NEGATIVE,
        "file_content_ttl": _DURATION,
        "max_size_mb": _POSITIVE,
    },
    "concurrency": {"max_parallel": _POSITIVE, "request_timeout": _POSITIVE},
    "drafts": {"submitted_retention": _DURATION},
}
_HOST_KEYS = frozenset({"hostname", "forge_type"})
_TABLE_SECTIONS = frozenset({"hosts", "plugins"})


def _type_name(value: object) -> str:
    """Name a TOML value's type without showing the value."""
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, int):
        return "an integer"
    if isinstance(value, float):
        return "a decimal number"
    if isinstance(value, str):
        return "a string"
    if isinstance(value, list):
        return "an array"
    if isinstance(value, dict):
        return "a table"
    if isinstance(value, (dt.date, dt.time)):
        return "a date or time"
    return "an unsupported value"


def _check_key(path: Path, name: str, value: object, rule: _KeyRule) -> None:
    """Raise ``ConfigFileError`` when ``value`` does not fit ``rule``."""
    if rule.kind == "duration":
        return
    if rule.kind == "boolean":
        if not isinstance(value, bool):
            raise ConfigFileError(
                path, f"{name} must be true or false, not {_type_name(value)}."
            )
        return
    if rule.kind == "string":
        if not isinstance(value, str):
            raise ConfigFileError(
                path,
                f"{name} must be a quoted string, not {_type_name(value)}.",
            )
        if rule.non_empty and not value.strip():
            raise ConfigFileError(path, f"{name} must not be empty.")
        return
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigFileError(
            path, f"{name} must be a whole number, not {_type_name(value)}."
        )
    if rule.minimum is not None and value < rule.minimum:
        bound = "greater than zero" if rule.minimum == 1 else "zero or more"
        raise ConfigFileError(path, f"{name} must be {bound}.")


def _check_hostname(path: Path, name: str, value: object) -> str:
    """Return a configured hostname, or raise naming what is wrong with it."""
    if not isinstance(value, str):
        raise ConfigFileError(
            path, f"{name} must be a quoted string, not {_type_name(value)}."
        )
    if not value:
        raise ConfigFileError(path, f"{name} must not be empty.")
    if "://" in value:
        problem = "must be a host name without a scheme, such as gitlab.example.com"
    elif "/" in value:
        problem = "must be a host name without a path, such as gitlab.example.com"
    elif value.startswith("["):
        problem = "must be a host name, not an IPv6 address"
    elif ":" in value:
        problem = "must be a host name without a port, such as gitlab.example.com"
    elif value != value.lower():
        problem = "must be lowercase"
    elif len(value) > 253 or _HOSTNAME_PATTERN.fullmatch(value) is None:
        problem = "must be a host name such as gitlab.example.com"
    else:
        return value
    raise ConfigFileError(path, f"{name} {problem}.")


def _shown(name: str) -> str:
    """Return a key or section name from the file safe to write to a log."""
    return _CONTROL_CHARACTERS.sub("?", name)


def _check_section(path: Path, section: str, table: dict[str, object]) -> None:
    rules = _SCHEMA[section]
    for key, value in table.items():
        rule = rules.get(key)
        if rule is None:
            log.warning(
                "Ignoring unknown key [%s] %s in %s", section, _shown(key), path
            )
            continue
        _check_key(path, f"[{section}] {key}", value, rule)


def _hosts(path: Path, hosts: dict[str, object]) -> dict[str, HostConfig]:
    result: dict[str, HostConfig] = {}
    for label, table in hosts.items():
        section = f"[hosts.{label}]"
        if not isinstance(table, dict):
            raise ConfigFileError(
                path, f"{section} must be a table, not {_type_name(table)}."
            )
        for key in table:
            if key not in _HOST_KEYS:
                log.warning(
                    "Ignoring unknown key %s %s in %s",
                    _shown(section),
                    _shown(key),
                    path,
                )
        if "hostname" not in table:
            raise ConfigFileError(path, f"{section} hostname is required.")
        hostname = _check_hostname(path, f"{section} hostname", table["hostname"])
        if "forge_type" not in table:
            raise ConfigFileError(
                path, f'{section} forge_type is required: use "github" or "gitlab".'
            )
        forge_type = table["forge_type"]
        if not isinstance(forge_type, str) or forge_type not in FORGE_TYPES:
            raise ConfigFileError(
                path, f'{section} forge_type must be "github" or "gitlab".'
            )
        result[label] = HostConfig(hostname=hostname, forge_type=forge_type)
    return result


def _plugins(path: Path, plugins: dict[str, object]) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for name, table in plugins.items():
        section = f"[plugins.{name}]"
        if not isinstance(table, dict):
            raise ConfigFileError(
                path, f"{section} must be a table, not {_type_name(table)}."
            )
        if "enabled" in table:
            _check_key(path, f"{section} enabled", table["enabled"], _BOOLEAN)
        result[name] = dict(table)
    return result


def _read_toml(path: Path) -> dict[str, object] | None:
    """Read and parse ``path``, or return None when it does not exist.

    Any other failure raises ``ConfigFileError``.
    """
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as error:
        reason = error.strerror or type(error).__name__
        raise ConfigFileError(path, f"the file cannot be read ({reason}).") from None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ConfigFileError(path, "the file is not valid UTF-8 text.") from None
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ConfigFileError(path, _syntax_problem(error)) from None


def _syntax_problem(error: tomllib.TOMLDecodeError) -> str:
    """Describe a TOML syntax error by its line and column, if it has them."""
    line = getattr(error, "lineno", None)
    column = getattr(error, "colno", None)
    reason = getattr(error, "msg", None)
    match = _TOML_POSITION.fullmatch(str(error))
    if match is not None:
        reason = reason if isinstance(reason, str) else match.group(1)
        line = line if isinstance(line, int) else int(match.group(2))
        column = column if isinstance(column, int) else int(match.group(3))
    if not isinstance(reason, str):
        reason = str(error)
    reason = reason.strip().rstrip(".") or "invalid TOML"
    if isinstance(line, int) and isinstance(column, int):
        return f"TOML syntax error at line {line}, column {column}: {reason}."
    return f"TOML syntax error: {reason}."


def load_config(path: Path | None = None) -> Config:
    """Load and validate config from a TOML file.

    A missing file means every default applies. A file that cannot be read or
    parsed, a value of the wrong type or out of range, or a malformed
    ``[hosts.<name>]`` table raises ``ConfigFileError``, a ``ConfigError``
    naming the file, section and key. Unknown sections and keys log one warning
    each and are ignored. The duration keys keep their own warn-and-default
    handling.
    """
    if path is None:
        path = config_dir() / "config.toml"

    raw = _read_toml(path)
    if raw is None:
        return Config()

    sections: dict[str, dict[str, object]] = {}
    for name, value in raw.items():
        if name not in _SCHEMA and name not in _TABLE_SECTIONS:
            if isinstance(value, dict):
                log.warning("Ignoring unknown section [%s] in %s", _shown(name), path)
            else:
                log.warning(
                    "Ignoring unknown top-level key %s in %s", _shown(name), path
                )
            continue
        if not isinstance(value, dict):
            raise ConfigFileError(
                path, f"[{name}] must be a table, not {_type_name(value)}."
            )
        sections[name] = value
        if name in _SCHEMA:
            _check_section(path, name, value)

    general = sections.get("general", {})
    editor = sections.get("editor", {})
    ui = sections.get("ui", {})
    cache = sections.get("cache", {})
    concurrency = sections.get("concurrency", {})
    drafts = sections.get("drafts", {})

    return Config(
        scan_root=general.get("scan_root", "~/git"),
        scan_depth=general.get("scan_depth", 5),
        editor_command=editor.get("command", ""),
        external_editor_enabled=editor.get("external_editor_enabled", True),
        theme=ui.get("theme", DEFAULT_THEME),
        syntax_theme=_syntax_theme(ui),
        diff_style=_diff_style(ui),
        show_draft_mrs=ui.get("show_draft_mrs", True),
        ascii_mode=ui.get("ascii_mode", False),
        color_scheme=_color_scheme(ui),
        mr_list_ttl=cache.get("mr_list_ttl", 60),
        diff_ttl=cache.get("diff_ttl", 300),
        max_cache_size_mb=cache.get("max_size_mb", 100),
        file_content_ttl=_file_content_ttl(cache),
        max_parallel=concurrency.get("max_parallel", 8),
        request_timeout=concurrency.get("request_timeout", 30),
        submitted_draft_retention=_submitted_retention(drafts),
        extra_hosts=_hosts(path, sections.get("hosts", {})),
        plugin_config=_plugins(path, sections.get("plugins", {})),
    )
