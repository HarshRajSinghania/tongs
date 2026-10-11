"""Configuration loader with platformdirs for cross-platform paths."""

from __future__ import annotations

import logging
import re
import tomllib
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import platformdirs

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
    theme: str = "monokai"
    diff_style: str = "unified"
    show_draft_mrs: bool = True
    ascii_mode: bool = False
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


def load_config(path: Path | None = None) -> Config:
    """Load config from TOML file. Returns defaults if file doesn't exist."""
    if path is None:
        path = config_dir() / "config.toml"

    if not path.exists():
        return Config()

    with open(path, "rb") as f:
        raw = tomllib.load(f)

    general = raw.get("general", {})
    editor = raw.get("editor", {})
    ui = raw.get("ui", {})
    cache = raw.get("cache", {})
    concurrency = raw.get("concurrency", {})
    drafts = raw.get("drafts", {})

    extra_hosts = {}
    for key, host_data in raw.get("hosts", {}).items():
        extra_hosts[key] = HostConfig(
            hostname=host_data.get("hostname", ""),
            forge_type=host_data.get("forge_type", ""),
        )

    plugin_config = {}
    for key, plugin_data in raw.get("plugins", {}).items():
        plugin_config[key] = dict(plugin_data)

    return Config(
        scan_root=general.get("scan_root", "~/git"),
        scan_depth=general.get("scan_depth", 5),
        editor_command=editor.get("command", ""),
        external_editor_enabled=editor.get("external_editor_enabled", True),
        theme=ui.get("theme", "monokai"),
        diff_style=ui.get("diff_style", "unified"),
        show_draft_mrs=ui.get("show_draft_mrs", True),
        ascii_mode=ui.get("ascii_mode", False),
        mr_list_ttl=cache.get("mr_list_ttl", 60),
        diff_ttl=cache.get("diff_ttl", 300),
        max_cache_size_mb=cache.get("max_size_mb", 100),
        file_content_ttl=_file_content_ttl(cache),
        max_parallel=concurrency.get("max_parallel", 8),
        request_timeout=concurrency.get("request_timeout", 30),
        submitted_draft_retention=_submitted_retention(drafts),
        extra_hosts=extra_hosts,
        plugin_config=plugin_config,
    )
