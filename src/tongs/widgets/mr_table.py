"""MR table widget and helper functions."""

from __future__ import annotations

from collections.abc import Iterable
from typing import ClassVar

from rich.text import Text
from textual.binding import Binding
from textual.widgets import DataTable
from textual.widgets.data_table import CellDoesNotExist

from tongs.forges.models import CIStatus, MRSummary
from tongs.helpers import ci_icon, forge_label, relative_time

MR_SORT_KEYS = ("updated", "title", "ci", "author")

_CI_PRIORITY = {
    CIStatus.FAILED: 0,
    CIStatus.RUNNING: 1,
    CIStatus.PENDING: 2,
    CIStatus.SUCCESS: 3,
    CIStatus.CANCELED: 4,
    CIStatus.SKIPPED: 5,
    CIStatus.UNKNOWN: 6,
}


def sort_mrs(mrs: list[MRSummary], key: str) -> list[MRSummary]:
    if key == "title":
        return sorted(mrs, key=lambda m: m.title.lower())
    if key == "ci":
        return sorted(
            mrs, key=lambda m: (_CI_PRIORITY.get(m.ci_status, 9), m.title.lower())
        )
    if key == "author":
        return sorted(mrs, key=lambda m: (m.author.username.lower(), m.title.lower()))
    return sorted(
        mrs, key=lambda m: m.updated_at or m.created_at or m.updated_at, reverse=True
    )


def _row_key(mr: MRSummary) -> str:
    return f"{mr.forge_host.hostname}:{mr.repo_path}:{mr.number}"


class MRTable(DataTable):
    """MR list table with consistent columns."""

    BINDINGS: ClassVar[list] = [
        Binding("s", "cycle_sort", "Sort", show=True, key_display="s"),
    ]

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._mr_data: dict[str, MRSummary] = {}
        self._sort_key: str = "updated"

    _show_repo: bool = True

    def setup_columns(self, show_repo: bool = True) -> None:
        self._show_repo = show_repo
        self.add_column("Forge", key="forge", width=5)
        self.add_column("CI", key="ci", width=4)
        self.add_column("#", key="number", width=6)
        self.add_column("Title", key="title")
        self.add_column("Author", key="author", width=14)
        if show_repo:
            self.add_column("Repo", key="repo", width=35)
        self.add_column("Updated", key="updated", width=8)

    def add_mr_row(self, mr: MRSummary, ascii_mode: bool = False) -> None:
        # Forge text is wrapped in Text so DataTable never parses it as markup:
        # a title such as "Fix [/] x" would raise MarkupError, and "[docs] y"
        # would silently lose its bracketed word. Only our own fixed labels
        # (forge and CI icons) go through the markup parser.
        title = Text()
        if mr.is_draft:
            title.append("D ", style="dim")
        else:
            title.append("  ")
        title.append(mr.title)
        row_key = _row_key(mr)
        self._mr_data[row_key] = mr
        row: list[Text] = [
            Text.from_markup(forge_label(mr.forge_host.forge_type)),
            Text.from_markup(ci_icon(mr.ci_status, ascii_mode)),
            Text(str(mr.number)),
            title,
            Text(mr.author.username),
        ]
        if self._show_repo:
            row.append(Text(mr.repo_path))
        row.append(Text(relative_time(mr.updated_at)))
        self.add_row(*row, key=row_key)

    def merge_mrs(
        self,
        mrs: Iterable[MRSummary],
        ascii_mode: bool = False,
        *,
        replace: bool = False,
    ) -> None:
        """Add rows in the active sort order, keeping the cursor on its MR.

        ``replace`` drops the current rows first. The highlighted MR keeps the
        cursor when it is still listed, wherever the new rows land. Rows that
        all sort after the current ones are appended without a redraw.
        """
        selected = self._selected_row_key()
        current = [] if replace else [row.key.value for row in self.ordered_rows]
        merged = {} if replace else dict(self._mr_data)
        incoming = {_row_key(mr): mr for mr in mrs}
        replaced = any(key in merged for key in incoming)
        merged.update(incoming)
        ordered = sort_mrs(list(merged.values()), self._sort_key)
        if not replace and not replaced:
            keys = [_row_key(mr) for mr in ordered]
            if keys[: len(current)] == current:
                for mr in ordered[len(current) :]:
                    self.add_mr_row(mr, ascii_mode)
                return
        self.clear()
        for mr in ordered:
            self.add_mr_row(mr, ascii_mode)
        if selected is not None and selected in self._mr_data:
            self.move_cursor(row=self.get_row_index(selected))

    def _selected_row_key(self) -> str | None:
        if self.row_count == 0:
            return None
        try:
            row_key, _ = self.coordinate_to_cell_key(self.cursor_coordinate)
        except CellDoesNotExist:
            return None
        return row_key.value

    def get_selected_mr(self) -> MRSummary | None:
        if self.cursor_row is None or self.row_count == 0:
            return None
        try:
            row_key, _ = self.coordinate_to_cell_key(self.cursor_coordinate)
            return self._mr_data.get(row_key.value)
        except CellDoesNotExist:
            return None

    def action_cycle_sort(self) -> None:
        idx = (
            MR_SORT_KEYS.index(self._sort_key) if self._sort_key in MR_SORT_KEYS else -1
        )
        self._sort_key = MR_SORT_KEYS[(idx + 1) % len(MR_SORT_KEYS)]
        self._rerender_sorted()
        self.app.notify(f"Sort: {self._sort_key}", timeout=2)

    def _rerender_sorted(self) -> None:
        mrs = list(self._mr_data.values())
        sorted_mrs = sort_mrs(mrs, self._sort_key)
        ascii_mode = getattr(self.app, "config", None) and self.app.config.ascii_mode
        super().clear()
        for mr in sorted_mrs:
            self.add_mr_row(mr, ascii_mode=bool(ascii_mode))

    def clear(self, *args, **kwargs) -> None:
        self._mr_data.clear()
        super().clear(*args, **kwargs)
