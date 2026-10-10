"""Two-press confirmation that expires and is bound to one target."""

from __future__ import annotations

import time
from collections.abc import Callable, Hashable

CONFIRM_TIMEOUT_SECONDS = 5.0
"""Seconds a first press stays armed before a second press re-arms instead."""


class TwoPressConfirm:
    """Confirm a destructive action only on a prompt second press.

    The first press arms the confirm for one target. A second press confirms
    only when it names the same target within the timeout; a late press or a
    press on a different target re-arms instead, so a stale first press never
    executes against whatever is selected later.
    """

    def __init__(
        self,
        timeout: float = CONFIRM_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._timeout = timeout
        self._clock = clock
        self._target: Hashable | None = None
        self._armed_at: float | None = None

    @property
    def armed(self) -> bool:
        """Is a first press waiting for its confirmation?"""
        return self._armed_at is not None

    def arm(self, target: Hashable) -> bool:
        """Record a press and return True only when it confirms the target."""
        now = self._clock()
        armed_at = self._armed_at
        if (
            armed_at is not None
            and self._target == target
            and 0.0 <= now - armed_at <= self._timeout
        ):
            self.reset()
            return True
        self._target = target
        self._armed_at = now
        return False

    def reset(self) -> None:
        """Drop any armed first press."""
        self._target = None
        self._armed_at = None


__all__ = ["CONFIRM_TIMEOUT_SECONDS", "TwoPressConfirm"]
