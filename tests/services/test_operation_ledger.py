"""Contract tests for the shared write-operation ledger policy."""

from __future__ import annotations

import asyncio

import pytest

from tongs.services.errors import ServiceError, ServiceErrorCode
from tongs.services.operation_ledger import (
    CAPACITY_EXHAUSTED_MESSAGE,
    OperationLedger,
    capacity_exhausted,
)

REVISION_CHANGED = ServiceError(
    ServiceErrorCode.REVISION_CHANGED,
    "The review revision changed. Refresh before writing.",
    retryable=True,
)


@pytest.mark.parametrize(
    ("capacity", "error"),
    [(0, ValueError), (-1, ValueError), (True, TypeError), (1.0, TypeError)],
)
def test_capacity_must_be_a_positive_integer(
    capacity: object, error: type[Exception]
) -> None:
    with pytest.raises(error):
        OperationLedger(capacity)  # type: ignore[arg-type]


def test_rejections_before_sending_do_not_fill_a_ledger_of_three() -> None:
    ledger: OperationLedger[str, str] = OperationLedger(3)

    for index in range(3):
        entry, owner = ledger.reserve(f"stale-{index}", "comment")
        assert owner is True
        ledger.release(entry, REVISION_CHANGED)
        with pytest.raises(ServiceError) as raised:
            OperationLedger.result(entry)
        assert raised.value is REVISION_CHANGED

    assert len(ledger) == 0
    for index in range(3):
        entry, owner = ledger.reserve(f"sent-{index}", "comment")
        assert owner is True
        OperationLedger.complete(entry, f"receipt-{index}")
    assert len(ledger) == 3


def test_full_ledger_asks_for_a_restart_and_is_not_retryable() -> None:
    ledger: OperationLedger[str, str] = OperationLedger(1)
    entry, _owner = ledger.reserve("sent", "comment")
    OperationLedger.complete(entry, "receipt")

    with pytest.raises(ServiceError) as raised:
        ledger.reserve("one-too-many", "comment")

    assert raised.value.code is ServiceErrorCode.CAPACITY_EXHAUSTED
    assert raised.value.code.value == "capacity_exhausted"
    assert raised.value.retryable is False
    assert raised.value.message == CAPACITY_EXHAUSTED_MESSAGE
    assert str(raised.value) == (
        "Too many write actions in this session. Restart tongs to continue."
    )
    assert capacity_exhausted() == raised.value
    repeated, owner = ledger.reserve("sent", "comment")
    assert repeated is entry
    assert owner is False


def test_sent_results_and_known_rejections_are_kept_without_eviction() -> None:
    ledger: OperationLedger[str, str] = OperationLedger(3)
    known, _ = ledger.reserve("known", "merge")
    unknown, _ = ledger.reserve("unknown", "close")
    refused, _ = ledger.reserve("refused", "reopen")
    OperationLedger.complete(known, "merged")
    OperationLedger.complete(unknown, "outcome unknown")
    refusal = ServiceError(ServiceErrorCode.PERMISSION_DENIED, "Denied.")
    OperationLedger.fail(refused, refusal)

    with pytest.raises(ServiceError):
        ledger.reserve("fourth", "merge")
    assert set(ledger) == {"known", "unknown", "refused"}
    assert OperationLedger.result(ledger["known"]) == "merged"
    assert OperationLedger.result(ledger["unknown"]) == "outcome unknown"
    with pytest.raises(ServiceError) as raised:
        OperationLedger.result(ledger["refused"])
    assert raised.value is refusal


def test_operation_id_binds_to_one_command() -> None:
    ledger: OperationLedger[str, str] = OperationLedger(2)
    ledger.reserve("bound", "merge")

    with pytest.raises(ServiceError) as raised:
        ledger.reserve("bound", "close")

    assert raised.value.code is ServiceErrorCode.CONFLICT
    assert raised.value.retryable is False
    assert len(ledger) == 1


def test_sent_result_is_never_released() -> None:
    ledger: OperationLedger[str, str] = OperationLedger(1)
    entry, _ = ledger.reserve("sent", "cancel")
    OperationLedger.retain(entry, "outcome unknown")

    ledger.release(entry, REVISION_CHANGED)

    assert ledger["sent"] is entry
    assert entry.error is None
    assert OperationLedger.complete(entry, "outcome unknown") == "outcome unknown"


@pytest.mark.asyncio
async def test_in_flight_duplicate_waits_and_shares_the_result() -> None:
    ledger: OperationLedger[str, str] = OperationLedger(1)
    entry, owner = ledger.reserve("in-flight", "retry")
    duplicate, duplicate_owner = ledger.reserve("in-flight", "retry")
    assert owner is True
    assert duplicate is entry
    assert duplicate_owner is False

    waiter = asyncio.create_task(OperationLedger.wait(duplicate))
    await asyncio.sleep(0)
    assert not waiter.done()
    OperationLedger.complete(entry, "retried")

    assert await waiter == "retried"


@pytest.mark.asyncio
async def test_in_flight_duplicate_gets_the_rejection_before_sending() -> None:
    ledger: OperationLedger[str, str] = OperationLedger(1)
    entry, _ = ledger.reserve("rejected", "retry")
    waiter = asyncio.create_task(OperationLedger.wait(entry))
    await asyncio.sleep(0)

    ledger.release(entry, REVISION_CHANGED)

    with pytest.raises(ServiceError) as raised:
        await waiter
    assert raised.value is REVISION_CHANGED
    retried, owner = ledger.reserve("rejected", "retry")
    assert retried is not entry
    assert owner is True
