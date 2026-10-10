"""One idempotency ledger policy for every session write service."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field

from tongs.services.errors import ServiceError, ServiceErrorCode

CAPACITY_EXHAUSTED_MESSAGE = (
    "Too many write actions in this session. Restart tongs to continue."
)


@dataclass(slots=True, eq=False)
class OperationEntry[CommandT, ReceiptT]:
    """The ledger state of one operation ID.

    ``receipt`` holds the result once the forge request was sent, and ``error``
    a definite rejection. ``done`` is set once either is final, which releases
    every request that waited on the same operation.
    """

    operation_id: str
    command: CommandT
    done: asyncio.Event = field(default_factory=asyncio.Event)
    receipt: ReceiptT | None = None
    error: ServiceError | None = None


class OperationLedger[CommandT, ReceiptT](
    Mapping[str, OperationEntry[CommandT, ReceiptT]]
):
    """Bind operation IDs to write commands for the life of one session.

    The policy is the same for every write service:

    - An operation ID binds to one command. The same ID with another command
      is a conflict.
    - A repeat of an operation that is still running waits for the first
      attempt and returns its result; nothing is sent twice.
    - A command rejected before any forge request is sent (a changed revision,
      invalid input, a cancel before sending) is released: it keeps no entry,
      and the same operation ID may be tried again.
    - A command that was sent keeps its known result, known rejection or
      unknown outcome for the rest of the session. Entries are never evicted,
      because a forgotten unknown outcome could be sent again.
    - A full ledger refuses new operations with a non-retryable
      ``CAPACITY_EXHAUSTED`` error, since only a new session frees space.

    The ledger is not thread-safe. Call it from one event loop, and make the
    reservation under the service's admission lock.
    """

    def __init__(self, capacity: int) -> None:
        if not isinstance(capacity, int) or isinstance(capacity, bool):
            raise TypeError("capacity must be an integer")
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._capacity = capacity
        self._entries: dict[str, OperationEntry[CommandT, ReceiptT]] = {}

    @property
    def capacity(self) -> int:
        return self._capacity

    def __getitem__(self, operation_id: str) -> OperationEntry[CommandT, ReceiptT]:
        return self._entries[operation_id]

    def __iter__(self) -> Iterator[str]:
        return iter(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def reserve(
        self, operation_id: str, command: CommandT
    ) -> tuple[OperationEntry[CommandT, ReceiptT], bool]:
        """Return the entry for ``operation_id`` and whether the caller owns it.

        The owner runs the command; any other caller awaits :meth:`wait`.
        """
        existing = self._entries.get(operation_id)
        if existing is not None:
            if existing.command != command:
                raise ServiceError(
                    ServiceErrorCode.CONFLICT,
                    "The operation ID is already bound to a different action.",
                )
            return existing, False
        if len(self._entries) >= self._capacity:
            raise capacity_exhausted()
        entry: OperationEntry[CommandT, ReceiptT] = OperationEntry(
            operation_id, command
        )
        self._entries[operation_id] = entry
        return entry, True

    @staticmethod
    def retain(entry: OperationEntry[CommandT, ReceiptT], receipt: ReceiptT) -> None:
        """Record the result of a sent command before the owner finishes it."""
        if not entry.done.is_set():
            entry.receipt = receipt

    @staticmethod
    def complete(
        entry: OperationEntry[CommandT, ReceiptT], receipt: ReceiptT
    ) -> ReceiptT:
        """Finish a sent command with its result and return the kept result."""
        if not entry.done.is_set():
            entry.receipt = receipt
            entry.done.set()
        return OperationLedger.result(entry)

    @staticmethod
    def fail(entry: OperationEntry[CommandT, ReceiptT], error: ServiceError) -> None:
        """Finish a sent command that the forge rejected with a known result."""
        if not entry.done.is_set():
            entry.error = error
            entry.done.set()

    def release(
        self, entry: OperationEntry[CommandT, ReceiptT], error: ServiceError
    ) -> None:
        """Forget a command rejected before any forge request was sent.

        Requests already waiting on it receive ``error``. An entry that holds a
        sent result is never released.
        """
        if entry.done.is_set() or entry.receipt is not None:
            return
        if self._entries.get(entry.operation_id) is entry:
            del self._entries[entry.operation_id]
        entry.error = error
        entry.done.set()

    @staticmethod
    async def wait(entry: OperationEntry[CommandT, ReceiptT]) -> ReceiptT:
        """Wait for the owner of ``entry`` and return or raise its result."""
        await entry.done.wait()
        return OperationLedger.result(entry)

    @staticmethod
    def result(entry: OperationEntry[CommandT, ReceiptT]) -> ReceiptT:
        """Return the final result of ``entry`` or raise its rejection."""
        if entry.error is not None:
            raise entry.error
        if entry.receipt is None:
            raise RuntimeError("completed operation has no result")
        return entry.receipt


def capacity_exhausted() -> ServiceError:
    """Return the error for a session that has no write capacity left."""
    return ServiceError(
        ServiceErrorCode.CAPACITY_EXHAUSTED,
        CAPACITY_EXHAUSTED_MESSAGE,
        retryable=False,
    )


__all__ = [
    "CAPACITY_EXHAUSTED_MESSAGE",
    "OperationEntry",
    "OperationLedger",
    "capacity_exhausted",
]
