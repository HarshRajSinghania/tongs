"""Tests for the expiring, target-bound two-press confirm."""

from __future__ import annotations

from tongs.widgets.two_press import CONFIRM_TIMEOUT_SECONDS, TwoPressConfirm


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_second_press_on_the_same_target_within_the_window_confirms() -> None:
    clock = _Clock()
    confirm = TwoPressConfirm(clock=clock)

    assert confirm.arm("merge") is False
    assert confirm.armed
    clock.now += 4.9
    assert confirm.arm("merge") is True
    assert not confirm.armed


def test_confirm_is_spent_after_it_fires() -> None:
    confirm = TwoPressConfirm(clock=_Clock())

    assert confirm.arm("merge") is False
    assert confirm.arm("merge") is True
    # A third press starts over instead of firing again.
    assert confirm.arm("merge") is False


def test_late_second_press_rearms_instead_of_confirming() -> None:
    clock = _Clock()
    confirm = TwoPressConfirm(clock=clock)

    assert confirm.arm("merge") is False
    clock.now += CONFIRM_TIMEOUT_SECONDS + 0.01
    assert confirm.arm("merge") is False
    assert confirm.armed
    # The late press re-armed, so a prompt press after it confirms.
    clock.now += 1.0
    assert confirm.arm("merge") is True


def test_press_exactly_at_the_timeout_still_confirms() -> None:
    clock = _Clock()
    confirm = TwoPressConfirm(timeout=2.0, clock=clock)

    assert confirm.arm("close") is False
    clock.now += 2.0
    assert confirm.arm("close") is True


def test_press_on_a_different_target_rearms_for_that_target() -> None:
    confirm = TwoPressConfirm(clock=_Clock())

    assert confirm.arm(("cancel", "pipeline", 7)) is False
    assert confirm.arm(("cancel", "job", 7)) is False
    # The first target was replaced, so it no longer confirms.
    assert confirm.arm(("cancel", "pipeline", 7)) is False
    assert confirm.arm(("cancel", "pipeline", 7)) is True


def test_reset_drops_the_armed_press() -> None:
    confirm = TwoPressConfirm(clock=_Clock())

    assert confirm.arm("thread-1") is False
    confirm.reset()
    assert not confirm.armed
    assert confirm.arm("thread-1") is False
    assert confirm.arm("thread-1") is True


def test_default_timeout_is_five_seconds() -> None:
    assert CONFIRM_TIMEOUT_SECONDS == 5.0
    clock = _Clock()
    confirm = TwoPressConfirm(clock=clock)

    assert confirm.arm("approve") is False
    clock.now += 5.5
    assert confirm.arm("approve") is False
