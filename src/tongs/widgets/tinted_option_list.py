"""Option list whose rows carry diff tints over Textual's own row rendering."""

from __future__ import annotations

from typing import Any

from rich.color import Color
from rich.segment import Segment
from rich.style import Style
from textual.strip import Strip
from textual.widgets import OptionList
from textual.widgets.option_list import Option

from tongs.diff.models import LineType

ADDITION_BACKGROUND = Color.from_rgb(0, 40, 0)
"""Row background for an added line."""

DELETION_BACKGROUND = Color.from_rgb(40, 0, 0)
"""Row background for a deleted line."""

SELECTION_BACKGROUND = Color.from_rgb(0, 50, 100)
"""Row background for a line inside a multi-line selection."""

_ADDITION_TINT = Style(bgcolor=ADDITION_BACKGROUND)
_DELETION_TINT = Style(bgcolor=DELETION_BACKGROUND)
_SELECTION_TINT = Style(bgcolor=SELECTION_BACKGROUND)


def strip_option_index(strip: Strip) -> int | None:
    """Return the option index Textual stamped on a rendered row, if any.

    ``OptionList`` stamps every segment of an option's rows with the
    ``{"option": index}`` meta it also reads for clicks and hover, so this is
    the one place the diff widgets learn which option a screen row belongs to.
    Blank rows past the last option carry no meta.
    """
    for segment in strip:
        style = segment.style
        if style is None:
            continue
        index = style.meta.get("option")
        if isinstance(index, int):
            return index
    return None


class TintedOptionList(OptionList):
    """An ``OptionList`` that paints addition, deletion and selection rows.

    Textual renders each row with the cursor, hover and disabled styles as
    usual; this class then replaces the row background when the option is an
    added or deleted line or lies inside the selection. The cursor row keeps
    Textual's highlight background so it stays visible on tinted lines.
    Subclasses fill ``_line_types`` and may override ``_in_selection_range``.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._line_types: dict[int, LineType] = {}

    def _in_selection_range(self, option_index: int) -> bool:
        """Whether an option lies inside the active multi-line selection."""
        return False

    def row_tint(self, option_index: int, option: Option) -> Style | None:
        """Return the background that replaces the row's own, or None."""
        if self.highlighted == option_index and not option.disabled:
            return None
        if self._in_selection_range(option_index):
            return _SELECTION_TINT
        if option.disabled:
            return None
        line_type = self._line_types.get(option_index)
        if line_type is LineType.ADDITION:
            return _ADDITION_TINT
        if line_type is LineType.DELETION:
            return _DELETION_TINT
        return None

    def render_line(self, y: int) -> Strip:
        strip = super().render_line(y)
        option_index = strip_option_index(strip)
        if option_index is None or not 0 <= option_index < self.option_count:
            return strip
        tint = self.row_tint(option_index, self.get_option_at_index(option_index))
        if tint is None:
            return strip
        return Strip(
            Segment.apply_style(list(strip), post_style=tint), strip.cell_length
        )
