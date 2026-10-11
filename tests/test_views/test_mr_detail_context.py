"""MR detail screen: reading a file at the displayed head to expand gaps."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import pytest

from tests.test_diff.test_context import FIXTURES
from tests.test_tui_mr_services import _app, _open_diff_tab, _settle
from tongs.app import TongsApp
from tongs.forges.models import MRSummary
from tongs.views.mr_detail import (
    CONTEXT_MISMATCH_MESSAGE,
    CONTEXT_STATUS_MESSAGES,
    MRDetailScreen,
)
from tongs.widgets.diff_panel import (
    READ_ONLY_LINE_MESSAGE,
    DiffOptionList,
    DiffPanel,
)


class _Status(StrEnum):
    """Mirrors the service's file read statuses by value."""

    OK = "ok"
    TOO_LARGE = "too_large"
    BINARY = "binary"
    NOT_FOUND = "not_found"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class _Text:
    lines: tuple[str, ...]
    ends_with_newline: bool
    byte_size: int


@dataclass(frozen=True, slots=True)
class _Read:
    status: _Status
    text: _Text | None
    message: str


def _split(text: str) -> _Text:
    parts = text.split("\n")
    ends_with_newline = parts[-1] == ""
    if ends_with_newline:
        parts.pop()
    return _Text(tuple(parts), ends_with_newline, len(text.encode()))


NEW_TEXT = _split(FIXTURES["two_hunks"][1])
OK = _Read(_Status.OK, NEW_TEXT, "")


class _Reads:
    """Fake adapter method that records every file read."""

    def __init__(self, reply: _Read | Exception = OK) -> None:
        self.calls: list[tuple[MRSummary, str, str]] = []
        self.reply = reply
        self.gate: asyncio.Event | None = None

    async def __call__(self, summary: MRSummary, path: str, ref: str) -> _Read:
        self.calls.append((summary, path, ref))
        if self.gate is not None:
            await self.gate.wait()
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def _install(tmp_path: Path, reads: _Reads) -> TongsApp:
    app, forge = _app(tmp_path)

    async def two_hunk_diff(repo_path: str, number: int) -> list[dict[str, object]]:
        return [
            {
                "filename": "widget.py",
                "status": "modified",
                "patch": FIXTURES["two_hunks"][2].rstrip("\n"),
                "additions": 2,
                "deletions": 2,
                "changes": 4,
            }
        ]

    forge.get_mr_diff_fresh = two_hunk_diff  # type: ignore[method-assign]
    app.services.read_review_file = reads  # type: ignore[attr-defined]
    app.forge = forge  # type: ignore[attr-defined]
    return app


async def _open(app: TongsApp, pilot: object) -> tuple[MRDetailScreen, DiffOptionList]:
    screen = await _open_diff_tab(app, pilot)
    option_list = screen.query_one(DiffOptionList)
    option_list.focus()
    await _settle(app)
    return screen, option_list


async def _press_e_on_gap(
    app: TongsApp, pilot: object, option_list: DiffOptionList, position: int
) -> None:
    option_list.highlighted = sorted(option_list._gap_rows)[position]
    await pilot.press("e")  # type: ignore[attr-defined]
    await _settle(app)


def _rows(option_list: DiffOptionList) -> list[str]:
    return [
        option_list.get_option_at_index(index).prompt.plain
        for index in range(option_list.option_count)
    ]


def _messages(app: TongsApp) -> list[str]:
    return [notification.message for notification in app._notifications]


@pytest.mark.asyncio
async def test_opening_makes_no_read_and_e_reads_the_head_once(
    tmp_path: Path,
) -> None:
    reads = _Reads()
    app = _install(tmp_path, reads)
    async with app.run_test(size=(120, 50), notifications=True) as pilot:
        screen, option_list = await _open(app, pilot)

        assert len(option_list._gap_rows) == 3
        assert reads.calls == []

        await _press_e_on_gap(app, pilot, option_list, 1)
        revision = screen._displayed_diff_revision
        assert revision is not None
        assert [(path, ref) for _, path, ref in reads.calls] == [
            ("widget.py", revision.head_sha)
        ]
        assert reads.calls[0][0] is screen.mr_summary
        rows = _rows(option_list)
        assert "  14   14    line 14" in rows
        cursor = option_list._readonly_rows[option_list.highlighted]
        assert cursor.new_lineno == 14

        await _press_e_on_gap(app, pilot, option_list, 0)
        await _press_e_on_gap(app, pilot, option_list, 0)
        assert len(reads.calls) == 1
        assert option_list._gap_rows == {}
        assert "   6    6    line 6" in _rows(option_list)
        assert "  30   30    line 30" in _rows(option_list)


@pytest.mark.asyncio
async def test_comment_on_an_expanded_line_is_refused_before_any_request(
    tmp_path: Path,
) -> None:
    reads = _Reads()
    app = _install(tmp_path, reads)
    async with app.run_test(size=(120, 50), notifications=True) as pilot:
        _, option_list = await _open(app, pilot)
        await _press_e_on_gap(app, pilot, option_list, 0)

        assert option_list.highlighted in option_list._readonly_rows
        await pilot.press("c")
        await _settle(app)

        assert READ_ONLY_LINE_MESSAGE in _messages(app)
        assert not any(call[0] == "inline" for call in app.forge.calls)  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        (
            _Read(_Status.TOO_LARGE, None, ""),
            CONTEXT_STATUS_MESSAGES["too_large"],
        ),
        (_Read(_Status.BINARY, None, ""), CONTEXT_STATUS_MESSAGES["binary"]),
        (_Read(_Status.NOT_FOUND, None, ""), CONTEXT_STATUS_MESSAGES["not_found"]),
        (
            _Read(_Status.UNSUPPORTED, None, ""),
            CONTEXT_STATUS_MESSAGES["unsupported"],
        ),
        (
            _Read(_Status.TOO_LARGE, None, "File too large to expand (over 2 MiB)."),
            "File too large to expand (over 2 MiB).",
        ),
        (
            _Read(
                _Status.OK,
                _Text(("drifted",) * 30, True, 240),
                "",
            ),
            CONTEXT_MISMATCH_MESSAGE,
        ),
        (RuntimeError("network down"), "Could not read the file. (network down)"),
    ],
)
@pytest.mark.asyncio
async def test_unreadable_files_say_why(
    tmp_path: Path, reply: _Read | Exception, expected: str
) -> None:
    reads = _Reads(reply)
    app = _install(tmp_path, reads)
    async with app.run_test(size=(120, 50), notifications=True) as pilot:
        _, option_list = await _open(app, pilot)

        await _press_e_on_gap(app, pilot, option_list, 0)

        assert expected in _messages(app)
        assert option_list._readonly_rows == {}
        assert len(option_list._gap_rows) == 3
        assert not any("loading" in row for row in _rows(option_list))
        assert all(message.isascii() for message in CONTEXT_STATUS_MESSAGES.values())
        assert CONTEXT_MISMATCH_MESSAGE.isascii()


@pytest.mark.asyncio
async def test_a_reply_after_a_refresh_is_ignored(tmp_path: Path) -> None:
    reads = _Reads()
    reads.gate = asyncio.Event()
    app = _install(tmp_path, reads)
    async with app.run_test(size=(120, 50), notifications=True) as pilot:
        screen, option_list = await _open(app, pilot)
        option_list.highlighted = min(option_list._gap_rows)
        await pilot.press("e")
        await pilot.pause()
        assert len(reads.calls) == 1
        assert any("loading" in row for row in _rows(option_list))

        screen.action_refresh()
        reads.gate.set()
        await _settle(app)

        assert option_list._readonly_rows == {}
        assert not any("loading" in row for row in _rows(option_list))
        assert not any("Could not read" in message for message in _messages(app))


@pytest.mark.asyncio
async def test_a_reply_for_a_replaced_diff_is_ignored(tmp_path: Path) -> None:
    reads = _Reads()
    reads.gate = asyncio.Event()
    app = _install(tmp_path, reads)
    async with app.run_test(size=(120, 50), notifications=True) as pilot:
        screen, option_list = await _open(app, pilot)
        option_list.highlighted = min(option_list._gap_rows)
        await pilot.press("e")
        await pilot.pause()
        old_revision = screen._displayed_diff_revision

        # The diff is reloaded with new file objects while the read waits.
        screen._load_diff()
        for _ in range(50):
            await pilot.pause()
            revision = screen._displayed_diff_revision
            if revision is not None and revision is not old_revision:
                break
        assert screen._displayed_diff_revision is not old_revision
        reads.gate.set()
        await _settle(app)

        assert option_list._readonly_rows == {}
        assert len(option_list._gap_rows) == 3
        panel = screen.query_one("#diff-panel", DiffPanel)
        assert panel.selection is not None
