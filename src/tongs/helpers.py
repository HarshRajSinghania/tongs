"""Shared utility helpers used across tongs widgets and views."""

from __future__ import annotations

from datetime import UTC, datetime

from rich.console import Console
from rich.markdown import Markdown
from rich.segment import Segment
from rich.style import Style

from tongs.config import DEFAULT_SYNTAX_THEME
from tongs.forges.models import CIStatus
from tongs.scanner.repo import ForgeType


def forge_label(forge_type: ForgeType | None) -> str:
    """Short GH / GL marker shared by the repo list and the inbox."""
    if forge_type == ForgeType.GITLAB:
        return "[blue]GL[/]"
    if forge_type == ForgeType.GITHUB:
        return "[white]GH[/]"
    return "[dim]--[/]"


def relative_time(dt: datetime | None) -> str:
    """Format a datetime as a relative time string (e.g., '3m ago')."""
    if dt is None:
        return ""
    now = datetime.now(UTC)
    delta = now - dt
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


def format_duration(seconds: float | None) -> str:
    """Format seconds as '45s', '2m 34s', '1h 05m'."""
    if seconds is None:
        return ""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    secs = seconds % 60
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours = minutes // 60
    mins = minutes % 60
    return f"{hours}h {mins:02d}m"


_CI_STYLES = {
    CIStatus.SUCCESS: ("●", Style(color="green")),
    CIStatus.FAILED: ("●", Style(color="red")),
    CIStatus.RUNNING: ("▶", Style(color="yellow")),
    CIStatus.PENDING: ("○", Style(dim=True)),
    CIStatus.CANCELED: ("—", Style(dim=True)),
    CIStatus.SKIPPED: ("—", Style(dim=True)),
    CIStatus.UNKNOWN: ("?", Style(dim=True)),
}

# The ``[ui] ascii_mode`` labels: words instead of glyphs, same colours.
_CI_ASCII = {
    CIStatus.SUCCESS: "OK",
    CIStatus.FAILED: "FAIL",
    CIStatus.RUNNING: "RUN",
    CIStatus.PENDING: "PEND",
    CIStatus.CANCELED: "CANC",
    CIStatus.SKIPPED: "SKIP",
    CIStatus.UNKNOWN: "?",
}

CI_ASCII_WIDTH = max(len(label) for label in _CI_ASCII.values())
"""The widest ASCII CI label, for lining up rows that start with one."""

_CI_UNKNOWN = ("?", Style(dim=True))


def ci_icon_text(status: CIStatus, ascii_mode: bool = False) -> tuple[str, Style]:
    """Return (character, Style) for a CI status icon, or its ASCII label."""
    glyph, style = _CI_STYLES.get(status, _CI_UNKNOWN)
    if ascii_mode:
        return _CI_ASCII.get(status, "?"), style
    return glyph, style


def ci_icon_markup(status: CIStatus, ascii_mode: bool = False) -> str:
    """Return Rich markup for a CI status icon, or its ASCII label."""
    glyph, style = ci_icon_text(status, ascii_mode)
    return f"[{style}]{glyph}[/]"


def ci_icon(status: CIStatus, ascii_mode: bool = False) -> str:
    """Return a Rich markup CI status icon (or ASCII fallback)."""
    return ci_icon_markup(status, ascii_mode)


# Glyphs Rich draws in Markdown even when it is told the output is ASCII.
_MARKDOWN_ASCII = str.maketrans({"•": "*", "▌": "|", "🌆": "#", "…": "..."})


def markdown_lines(
    body: str,
    width: int,
    *,
    ascii_mode: bool = False,
    code_theme: str = DEFAULT_SYNTAX_THEME,
) -> list[list[Segment]]:
    """Render Markdown with Rich into lines of segments ``width`` cells wide.

    With ``ascii_mode`` the headings, tables and rules use ASCII boxes and the
    list bullets and quote bars become ``*`` and ``|``. Fenced code uses the
    Pygments style ``code_theme``.
    """
    console = Console(width=80)
    options = console.options.update_width(width)
    markdown = Markdown(body, code_theme=code_theme)
    if not ascii_mode:
        return console.render_lines(markdown, options)
    options.encoding = "ascii"
    return [
        [
            Segment(segment.text.translate(_MARKDOWN_ASCII), segment.style)
            for segment in line
        ]
        for line in console.render_lines(markdown, options)
    ]
