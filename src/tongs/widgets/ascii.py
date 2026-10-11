"""ASCII stand-ins for the glyphs Textual draws, for ``[ui] ascii_mode``.

Minimal consoles and fonts show box-drawing, block and arrow characters as
empty boxes. With ``ascii_mode`` the app adds the ``-ascii`` CSS class, whose
rules redraw every border with ``+``, ``-`` and ``|``, and these pieces cover
the glyphs that CSS cannot reach: the header icon, tree arrows and guides,
Markdown bullets, scrollbar ends, the loading indicator and key names.
"""

from __future__ import annotations

from typing import Any, ClassVar

from rich.text import Text
from textual.app import RenderResult
from textual.binding import Binding
from textual.command import CommandPalette, SearchIcon
from textual.scrollbar import ScrollBarRender
from textual.widgets import Header, LoadingIndicator, Markdown

from tongs.config import app_config

ASCII_CLASS = "-ascii"
"""The app CSS class that ``[ui] ascii_mode`` sets."""

HEADER_ICON = "o"
"""The header's command palette icon, in place of a Unicode circle."""

TITLE_SEPARATOR = " - "
"""Between the title and the sub-title in the header."""

TREE_ICONS = ("> ", "v ")
"""Collapsed and expanded tree node arrows."""

TREE_LINES = {
    style: ("  ", "| ", "`-", "|-") for style in ("default", "bold", "double")
}
"""Tree guides for every guide style: space, vertical, last child, child."""

MARKDOWN_BULLETS = ["* ", "- ", "+ ", "* ", "- "]
"""Unordered list bullets by nesting depth."""

PALETTE_PLACEHOLDER = "Search for commands..."

THEME_PLACEHOLDER = "Search for themes..."

SEARCH_ICON = ">"
"""The command palette's prompt, in place of a magnifying glass."""

LOADING_TEXT = "Loading..."


class AsciiScrollBarRender(ScrollBarRender):
    """Scrollbars without the partial block glyphs at the ends of the thumb.

    The thumb is still drawn with its background colour, a whole cell at a
    time.
    """

    VERTICAL_BARS: ClassVar[list[str]] = [" "] * 8
    HORIZONTAL_BARS: ClassVar[list[str]] = [" "] * 8


class AsciiLoadingIndicator(LoadingIndicator):
    """A loading indicator that says so in words instead of animated dots."""

    def render(self) -> RenderResult:
        return Text(LOADING_TEXT)


class AsciiCommandPalette(CommandPalette):
    """The command palette with an ASCII prompt and placeholder."""

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("placeholder", PALETTE_PLACEHOLDER)
        super().__init__(**kwargs)

    def on_mount(self) -> None:
        self.query_one(SearchIcon).icon = SEARCH_ICON


class TongsMarkdown(Markdown):
    """A Markdown view whose list bullets follow ``[ui] ascii_mode``."""

    def on_mount(self) -> None:
        if app_config(self.app).ascii_mode:
            self.BULLETS = list(MARKDOWN_BULLETS)


def tongs_header(owner: object) -> Header:
    """Return a screen header, with an ASCII icon under ``[ui] ascii_mode``."""
    return Header(icon=HEADER_ICON if app_config(owner).ascii_mode else None)


def ascii_key_display(binding: Binding) -> str:
    """Name a binding's key in ASCII: ``enter`` and ``up`` instead of arrows.

    A control key keeps Textual's caret form, such as ``^r``.
    """
    modifiers, key = binding.parse_key()
    if not key.isascii():
        key = "?"
    if "ctrl" in modifiers:
        modifiers.remove("ctrl")
        key = f"^{key}"
    return "+".join([*modifiers, key])


__all__ = [
    "ASCII_CLASS",
    "HEADER_ICON",
    "LOADING_TEXT",
    "MARKDOWN_BULLETS",
    "PALETTE_PLACEHOLDER",
    "SEARCH_ICON",
    "THEME_PLACEHOLDER",
    "TITLE_SEPARATOR",
    "TREE_ICONS",
    "TREE_LINES",
    "AsciiCommandPalette",
    "AsciiLoadingIndicator",
    "AsciiScrollBarRender",
    "TongsMarkdown",
    "ascii_key_display",
    "tongs_header",
]
