"""Lifecycle and scoped-facade tests for installed desktop providers."""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from importlib.metadata import EntryPoint

import pytest

from tongs.plugins.desktop import (
    DesktopCallContext,
    DesktopCancellation,
    DesktopHostFacade,
    DesktopLocation,
    DesktopNotificationSeverity,
    DesktopPluginContext,
    DesktopPluginContractError,
    DesktopPluginErrorCode,
    DesktopPluginState,
    DesktopReadKind,
    FrozenJsonObject,
    FrozenJsonValue,
    freeze_json_object,
)
from tongs.plugins.desktop_registry import DesktopPluginRegistry


class RecordingFacade:
    def __init__(self) -> None:
        self.notifications: list[tuple[str, DesktopNotificationSeverity]] = []
        self.events: list[tuple[str, FrozenJsonObject]] = []
        self.focus_requests: list[tuple[str, FrozenJsonObject]] = []
        self.invocations: list[tuple[str, FrozenJsonObject, DesktopCallContext]] = []

    async def read(
        self,
        kind: DesktopReadKind,
        params: FrozenJsonObject,
        cancellation: DesktopCancellation,
    ) -> FrozenJsonValue:
        return {"kind": kind.value, "cancelled": cancellation.cancelled}

    async def invoke(
        self,
        method: str,
        params: FrozenJsonObject,
        context: DesktopCallContext,
    ) -> FrozenJsonValue:
        self.invocations.append((method, params, context))
        return {"method": method, "plugin": "bound"}

    async def notify(
        self,
        message: str,
        severity: DesktopNotificationSeverity = DesktopNotificationSeverity.INFORMATION,
    ) -> None:
        self.notifications.append((message, severity))

    async def publish_event(self, event_id: str, payload: FrozenJsonObject) -> None:
        self.events.append((event_id, payload))

    def current_location(self) -> DesktopLocation | None:
        return DesktopLocation(freeze_json_object({"screen": "review"}))

    async def focus(self, target_id: str, metadata: FrozenJsonObject) -> None:
        self.focus_requests.append((target_id, metadata))


def only_good(
    source: Callable[[str], Sequence[EntryPoint]],
) -> Callable[[str], Sequence[EntryPoint]]:
    return lambda group: tuple(item for item in source(group) if item.name == "good")


def good_and_dual(
    source: Callable[[str], Sequence[EntryPoint]],
) -> Callable[[str], Sequence[EntryPoint]]:
    return lambda group: tuple(
        item for item in source(group) if item.name in {"good", "dual"}
    )


def record(registry: DesktopPluginRegistry):
    return registry.plugins[0]


@pytest.mark.asyncio
async def test_start_call_lookup_and_stop_use_frozen_scoped_contracts(
    installed_entry_point_source: Callable[[str], Sequence[EntryPoint]],
) -> None:
    facade = RecordingFacade()
    registry = DesktopPluginRegistry(
        {
            "good": {
                "enabled": True,
                "nested": {"ready": True},
                "publish_on_start": True,
            }
        },
        entry_point_source=only_good(installed_entry_point_source),
        host_version="0.1.dev3+candidate",
    )
    registry.discover()

    await registry.start_all(lambda _plugin_id, _manifest: facade)
    result = await registry.call(
        "good",
        "echo",
        {"value": 42},
        DesktopCallContext("call-1", DesktopCancellation()),
    )

    assert record(registry).state is DesktopPluginState.STARTED
    assert result.ok
    assert result.value == {"method": "echo", "value": 42}
    assert registry.asset("good", "main").package == "fixture_desktop_assets"
    assert registry.command("good", "open").navigation_id == "review"
    assert registry.navigation("good", "review").module_id == "review"
    assert registry.focus_target("good", "editor").module_id == "review"
    assert isinstance(facade, DesktopHostFacade)
    assert facade.notifications == [
        ("Fixture started", DesktopNotificationSeverity.INFORMATION)
    ]
    assert facade.events == [("refreshed", {"ready": True})]
    providers = importlib.import_module("fixture_desktop_good").GoodProvider.instances
    assert providers[-1].context is not None
    assert providers[-1].context.config == {
        "nested": {"ready": True},
        "publish_on_start": True,
    }
    assert "enabled" not in providers[-1].context.config
    with pytest.raises(DesktopPluginContractError):
        registry.asset("good", "other")

    await registry.stop_all()
    assert record(registry).state is DesktopPluginState.STOPPED


@pytest.mark.asyncio
async def test_context_scopes_events_and_focus_to_manifest_declarations() -> None:
    facade = RecordingFacade()
    context = DesktopPluginContext(
        "good",
        freeze_json_object({}),
        facade,
        DesktopCancellation(),
        read_kinds=frozenset({DesktopReadKind.REVIEWS}),
        method_ids=frozenset({"echo"}),
        event_ids=frozenset({"refreshed"}),
        focus_target_ids=frozenset({"editor"}),
    )

    result = await context.read(
        DesktopReadKind.REVIEWS, {"state": "open"}, DesktopCancellation()
    )
    call_context = DesktopCallContext("local-call", DesktopCancellation())
    invoked = await context.invoke("echo", {"value": 1}, call_context)
    await context.notify("Ready", DesktopNotificationSeverity.WARNING)
    await context.publish_event("refreshed", {"count": 1})
    await context.focus("editor", {"line": 2})

    assert facade.events == [("refreshed", {"count": 1})]
    assert facade.focus_requests == [("editor", {"line": 2})]
    assert facade.notifications == [("Ready", DesktopNotificationSeverity.WARNING)]
    assert context.current_location() == DesktopLocation(
        freeze_json_object({"screen": "review"})
    )
    assert result == {"kind": "reviews", "cancelled": False}
    assert invoked == {"method": "echo", "plugin": "bound"}
    assert facade.invocations == [("echo", {"value": 1}, call_context)]
    with pytest.raises(DesktopPluginContractError):
        await context.read(DesktopReadKind.LOG, {}, DesktopCancellation())
    with pytest.raises(DesktopPluginContractError):
        await context.publish_event("other", {})
    with pytest.raises(DesktopPluginContractError):
        await context.focus("other", {})
    with pytest.raises(DesktopPluginContractError):
        await context.invoke("other.echo", {}, call_context)


@pytest.mark.asyncio
async def test_call_rejects_undeclared_duplicate_and_unbounded_data(
    installed_entry_point_source: Callable[[str], Sequence[EntryPoint]],
) -> None:
    registry = DesktopPluginRegistry(
        entry_point_source=only_good(installed_entry_point_source),
        host_version="1.0",
    )
    registry.discover()
    await registry.start_all(lambda _plugin_id, _manifest: RecordingFacade())

    undeclared = await registry.call(
        "good", "secret", {}, DesktopCallContext("call-1", DesktopCancellation())
    )
    invalid_id = await registry.call(
        "good", "echo", {}, DesktopCallContext("bad id", DesktopCancellation())
    )
    invalid_data = await registry.call(
        "good",
        "echo",
        {"value": {1, 2}},  # type: ignore[dict-item]
        DesktopCallContext("call-2", DesktopCancellation()),
    )

    assert undeclared.error.code is DesktopPluginErrorCode.UNDECLARED_METHOD  # type: ignore[union-attr]
    assert invalid_id.error.code is DesktopPluginErrorCode.INVALID_DATA  # type: ignore[union-attr]
    assert invalid_data.error.code is DesktopPluginErrorCode.INVALID_DATA  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_concurrent_duplicate_invocation_id_is_rejected(
    installed_entry_point_source: Callable[[str], Sequence[EntryPoint]],
) -> None:
    registry = DesktopPluginRegistry(
        entry_point_source=only_good(installed_entry_point_source),
        host_version="1.0",
    )
    registry.discover()
    await registry.start_all(lambda _plugin_id, _manifest: RecordingFacade())
    cancellation = DesktopCancellation()
    first = asyncio.create_task(
        registry.call("good", "wait", {}, DesktopCallContext("same-id", cancellation))
    )
    await asyncio.sleep(0)

    duplicate = await registry.call(
        "good",
        "echo",
        {},
        DesktopCallContext("same-id", DesktopCancellation()),
    )
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    assert duplicate.error.code is DesktopPluginErrorCode.INVALID_DATA  # type: ignore[union-attr]
    assert cancellation.cancelled


@pytest.mark.asyncio
async def test_timed_out_call_retains_id_until_provider_task_finishes(
    installed_entry_point_source: Callable[[str], Sequence[EntryPoint]],
) -> None:
    registry = DesktopPluginRegistry(
        entry_point_source=only_good(installed_entry_point_source),
        host_version="1.0",
        call_timeout_seconds=0.005,
    )
    registry.discover()
    await registry.start_all(lambda _plugin_id, _manifest: RecordingFacade())

    timed_out = await registry.call(
        "good",
        "stubborn_wait",
        {},
        DesktopCallContext("retained-id", DesktopCancellation()),
    )
    duplicate = await registry.call(
        "good",
        "echo",
        {},
        DesktopCallContext("retained-id", DesktopCancellation()),
    )

    assert timed_out.error.code is DesktopPluginErrorCode.CALL_TIMEOUT  # type: ignore[union-attr]
    assert duplicate.error.code is DesktopPluginErrorCode.INVALID_DATA  # type: ignore[union-attr]
    async with asyncio.timeout(2):
        while True:
            recovery = await registry.call(
                "good",
                "echo",
                {},
                DesktopCallContext("retained-id", DesktopCancellation()),
            )
            if recovery.ok:
                break
            assert recovery.error.code is DesktopPluginErrorCode.INVALID_DATA  # type: ignore[union-attr]
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_stop_cancels_and_drains_pending_call_tasks(
    installed_entry_point_source: Callable[[str], Sequence[EntryPoint]],
) -> None:
    registry = DesktopPluginRegistry(
        entry_point_source=only_good(installed_entry_point_source),
        host_version="1.0",
        call_timeout_seconds=10,
    )
    registry.discover()
    await registry.start_all(lambda _plugin_id, _manifest: RecordingFacade())
    pending = asyncio.create_task(
        registry.call(
            "good",
            "wait",
            {},
            DesktopCallContext("pending", DesktopCancellation()),
        )
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    await registry.stop_all()

    assert pending.done()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert record(registry).state is DesktopPluginState.STOPPED


@pytest.mark.asyncio
async def test_noncooperative_pending_call_reports_cleanup_timeout(
    installed_entry_point_source: Callable[[str], Sequence[EntryPoint]],
) -> None:
    registry = DesktopPluginRegistry(
        entry_point_source=only_good(installed_entry_point_source),
        host_version="1.0",
        call_timeout_seconds=10,
        cleanup_timeout_seconds=0.005,
    )
    registry.discover()
    await registry.start_all(lambda _plugin_id, _manifest: RecordingFacade())
    pending = asyncio.create_task(
        registry.call(
            "good",
            "stubborn_wait",
            {},
            DesktopCallContext("stubborn", DesktopCancellation()),
        )
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    await registry.stop_all()

    assert pending.done()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert record(registry).state is DesktopPluginState.FAILED
    assert record(registry).error.code is DesktopPluginErrorCode.CLEANUP_TIMEOUT  # type: ignore[union-attr]
    await asyncio.sleep(0.06)


@pytest.mark.asyncio
async def test_stop_rejects_calls_started_after_cleanup_begins(
    installed_entry_point_source: Callable[[str], Sequence[EntryPoint]],
) -> None:
    registry = DesktopPluginRegistry(
        {"good": {"hang_stop": True}},
        entry_point_source=only_good(installed_entry_point_source),
        host_version="1.0",
        cleanup_timeout_seconds=0.005,
    )
    registry.discover()
    await registry.start_all(lambda _plugin_id, _manifest: RecordingFacade())
    stopping = asyncio.create_task(registry.stop_all())
    await asyncio.sleep(0)

    call = await registry.call(
        "good",
        "echo",
        {},
        DesktopCallContext("late-call", DesktopCancellation()),
    )
    await stopping

    assert call.error.code is DesktopPluginErrorCode.UNAVAILABLE  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_call_failure_timeout_and_invalid_result_are_isolated(
    installed_entry_point_source: Callable[[str], Sequence[EntryPoint]],
) -> None:
    registry = DesktopPluginRegistry(
        entry_point_source=only_good(installed_entry_point_source),
        host_version="1.0",
        call_timeout_seconds=0.01,
    )
    registry.discover()
    await registry.start_all(lambda _plugin_id, _manifest: RecordingFacade())

    failure = await registry.call(
        "good", "explode", {}, DesktopCallContext("failure", DesktopCancellation())
    )
    cancellation = DesktopCancellation()
    timeout = await registry.call(
        "good", "wait", {}, DesktopCallContext("timeout", cancellation)
    )
    invalid = await registry.call(
        "good", "invalid", {}, DesktopCallContext("invalid", DesktopCancellation())
    )
    recovery = await registry.call(
        "good",
        "echo",
        {"value": "ready"},
        DesktopCallContext("recovery", DesktopCancellation()),
    )

    assert failure.error.code is DesktopPluginErrorCode.CALL_FAILED  # type: ignore[union-attr]
    assert timeout.error.code is DesktopPluginErrorCode.CALL_TIMEOUT  # type: ignore[union-attr]
    assert cancellation.cancelled
    assert invalid.error.code is DesktopPluginErrorCode.INVALID_DATA  # type: ignore[union-attr]
    assert recovery.ok


@pytest.mark.asyncio
async def test_provider_observes_call_cancellation_signal(
    installed_entry_point_source: Callable[[str], Sequence[EntryPoint]],
) -> None:
    registry = DesktopPluginRegistry(
        entry_point_source=only_good(installed_entry_point_source),
        host_version="1.0",
    )
    registry.discover()
    await registry.start_all(lambda _plugin_id, _manifest: RecordingFacade())
    cancellation = DesktopCancellation()
    cancellation.cancel()

    result = await registry.call(
        "good",
        "observe_cancellation",
        {},
        DesktopCallContext("observe", cancellation),
    )

    assert result.value == {"cancelled": True}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("config", "expected_code"),
    [
        ({"fail_stop": True}, DesktopPluginErrorCode.STOP_FAILED),
        ({"hang_stop": True}, DesktopPluginErrorCode.CLEANUP_TIMEOUT),
    ],
)
async def test_cleanup_failure_is_bounded_and_retained(
    installed_entry_point_source: Callable[[str], Sequence[EntryPoint]],
    config: dict[str, bool],
    expected_code: DesktopPluginErrorCode,
) -> None:
    registry = DesktopPluginRegistry(
        {"good": config},
        entry_point_source=only_good(installed_entry_point_source),
        host_version="1.0",
        cleanup_timeout_seconds=0.01,
    )
    registry.discover()
    await registry.start_all(lambda _plugin_id, _manifest: RecordingFacade())

    await registry.stop_all()

    assert record(registry).state is DesktopPluginState.FAILED
    assert record(registry).error.code is expected_code  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_stop_after_timed_out_call_preserves_returned_caller(
    installed_entry_point_source: Callable[[str], Sequence[EntryPoint]],
) -> None:
    registry = DesktopPluginRegistry(
        {},
        entry_point_source=only_good(installed_entry_point_source),
        host_version="1.0",
        call_timeout_seconds=0.005,
    )
    registry.discover()
    await registry.start_all(lambda _plugin_id, _manifest: RecordingFacade())
    caller = asyncio.current_task()
    assert caller is not None
    cancellations = caller.cancelling()

    result = await registry.call(
        "good",
        "stubborn_wait",
        {},
        DesktopCallContext("returned-call", DesktopCancellation()),
    )
    assert result.error is not None
    assert result.error.code is DesktopPluginErrorCode.CALL_TIMEOUT

    await registry.stop_all()
    await asyncio.sleep(0)

    assert caller.cancelling() == cancellations
    assert record(registry).state is DesktopPluginState.STOPPED
    assert not registry._runtimes["good"].provider_tasks
    assert not registry._runtimes["good"].calls


@pytest.mark.asyncio
async def test_cleanup_deadline_does_not_await_suppressed_task_cancellation(
    installed_entry_point_source: Callable[[str], Sequence[EntryPoint]],
) -> None:
    registry = DesktopPluginRegistry(
        {"good": {"stubborn_stop": True}},
        entry_point_source=only_good(installed_entry_point_source),
        host_version="1.0",
        cleanup_timeout_seconds=0.005,
    )
    registry.discover()
    await registry.start_all(lambda _plugin_id, _manifest: RecordingFacade())
    loop = asyncio.get_running_loop()
    started = loop.time()

    await registry.stop_all()

    assert loop.time() - started < 0.04
    assert record(registry).error.code is DesktopPluginErrorCode.CLEANUP_TIMEOUT  # type: ignore[union-attr]
    await asyncio.sleep(0.06)


@pytest.mark.asyncio
async def test_start_failure_isolated_and_not_erased_by_cleanup(
    installed_entry_point_source: Callable[[str], Sequence[EntryPoint]],
) -> None:
    registry = DesktopPluginRegistry(
        {"good": {"fail_start": True}},
        entry_point_source=only_good(installed_entry_point_source),
        host_version="1.0",
    )
    registry.discover()

    await registry.start_all(lambda _plugin_id, _manifest: RecordingFacade())
    await registry.stop_all()

    assert record(registry).state is DesktopPluginState.FAILED
    assert record(registry).error.code is DesktopPluginErrorCode.START_FAILED  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_start_timeout_signals_lifecycle_cancellation(
    installed_entry_point_source: Callable[[str], Sequence[EntryPoint]],
) -> None:
    registry = DesktopPluginRegistry(
        {"good": {"hang_start": True}},
        entry_point_source=only_good(installed_entry_point_source),
        host_version="1.0",
        start_timeout_seconds=0.01,
    )
    providers = importlib.import_module("fixture_desktop_good").GoodProvider.instances
    already_created = len(providers)
    registry.discover()

    await registry.start_all(lambda _plugin_id, _manifest: RecordingFacade())

    assert record(registry).state is DesktopPluginState.FAILED
    assert record(registry).error.code is DesktopPluginErrorCode.START_FAILED  # type: ignore[union-attr]
    (provider,) = providers[already_created:]
    assert provider.context is not None
    assert provider.context.cancellation.cancelled


@pytest.mark.asyncio
async def test_provider_cancelled_start_does_not_skip_other_providers(
    installed_entry_point_source: Callable[[str], Sequence[EntryPoint]],
) -> None:
    registry = DesktopPluginRegistry(
        {"good": {"cancel_start": True}},
        entry_point_source=good_and_dual(installed_entry_point_source),
        host_version="1.0",
    )
    registry.discover()

    await registry.start_all(lambda _plugin_id, _manifest: RecordingFacade())

    states = {item.plugin_id: item for item in registry.plugins}
    assert states["dual"].state is DesktopPluginState.STARTED
    assert states["good"].state is DesktopPluginState.FAILED
    assert states["good"].error.code is DesktopPluginErrorCode.START_FAILED  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_provider_cancelled_stop_does_not_skip_other_providers(
    installed_entry_point_source: Callable[[str], Sequence[EntryPoint]],
) -> None:
    registry = DesktopPluginRegistry(
        {"good": {"cancel_stop": True}},
        entry_point_source=good_and_dual(installed_entry_point_source),
        host_version="1.0",
    )
    registry.discover()
    await registry.start_all(lambda _plugin_id, _manifest: RecordingFacade())

    await registry.stop_all()

    states = {item.plugin_id: item for item in registry.plugins}
    assert states["dual"].state is DesktopPluginState.STOPPED
    assert states["good"].state is DesktopPluginState.FAILED
    assert states["good"].error.code is DesktopPluginErrorCode.STOP_FAILED  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_stopping_registry_signals_plugin_cancellation_token(
    installed_entry_point_source: Callable[[str], Sequence[EntryPoint]],
) -> None:
    registry = DesktopPluginRegistry(
        entry_point_source=only_good(installed_entry_point_source),
        host_version="1.0",
    )
    providers = importlib.import_module("fixture_desktop_good").GoodProvider.instances
    already_created = len(providers)
    registry.discover()
    await registry.start_all(lambda _plugin_id, _manifest: RecordingFacade())
    (provider,) = providers[already_created:]
    assert provider.context is not None
    assert not provider.context.cancellation.cancelled

    await registry.stop_all()

    assert provider.context.cancellation.cancelled


@pytest.mark.asyncio
async def test_cancelling_outer_task_during_call_signals_cancellation_token(
    installed_entry_point_source: Callable[[str], Sequence[EntryPoint]],
) -> None:
    registry = DesktopPluginRegistry(
        entry_point_source=only_good(installed_entry_point_source),
        host_version="1.0",
    )
    registry.discover()
    await registry.start_all(lambda _plugin_id, _manifest: RecordingFacade())

    cancellation = DesktopCancellation()
    call_context = DesktopCallContext("c-outer-cancel", cancellation)

    call_task = asyncio.create_task(registry.call("good", "wait", {}, call_context))
    await asyncio.sleep(0.01)
    assert not cancellation.cancelled

    call_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call_task

    assert cancellation.cancelled


def test_facade_protocol_has_no_concrete_host_or_tui_capabilities() -> None:
    public_names = {
        name for name in DesktopHostFacade.__dict__ if not name.startswith("_")
    }

    assert public_names == {
        "read",
        "invoke",
        "notify",
        "publish_event",
        "current_location",
        "focus",
    }
    assert public_names.isdisjoint(
        {"forge_registry", "cache", "token", "client", "app", "query_one"}
    )


@dataclass(frozen=True, slots=True)
class _StaticEntryPoint:
    """An in-memory desktop entry point that loads a test provider class."""

    name: str
    target: type
    group: str = "tongs.desktop_plugins"

    def load(self) -> type:
        return self.target


class _TimedProvider:
    """A provider whose start and stop behavior comes from its plugin config."""

    def __init__(self, plugin_id: str, log: list[tuple[str, str, float]]) -> None:
        self.plugin_id = plugin_id
        self.log = log
        self.context: DesktopPluginContext | None = None

    def manifest(self):
        fixture = importlib.import_module("fixture_desktop_good")
        return fixture.make_manifest(self.plugin_id)

    def _note(self, event: str) -> None:
        self.log.append((event, self.plugin_id, asyncio.get_running_loop().time()))

    async def start(self, context: DesktopPluginContext) -> None:
        self.context = context
        self._note("start")
        behavior = context.config.get("start")
        if behavior == "hang":
            await asyncio.Event().wait()
        elif behavior == "stubborn":
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await asyncio.sleep(0.05)
        elif isinstance(behavior, float):
            await asyncio.sleep(behavior)
        self._note("started")

    async def call(
        self, method: str, params: FrozenJsonObject, context: DesktopCallContext
    ) -> FrozenJsonValue:
        return {}

    async def stop(self) -> None:
        assert self.context is not None
        self._note("stop")
        behavior = self.context.config.get("stop")
        if behavior == "hang":
            await asyncio.Event().wait()
        elif isinstance(behavior, float):
            await asyncio.sleep(behavior)
        self._note("stopped")


def _timed_registry(
    config: dict[str, dict[str, object]],
    log: list[tuple[str, str, float]],
    providers: list[_TimedProvider],
    **timeouts: float,
) -> DesktopPluginRegistry:
    def provider_class(plugin_id: str) -> type:
        class Provider(_TimedProvider):
            def __init__(self) -> None:
                super().__init__(plugin_id, log)
                providers.append(self)

        return Provider

    entries = tuple(
        _StaticEntryPoint(plugin_id, provider_class(plugin_id)) for plugin_id in config
    )
    return DesktopPluginRegistry(
        config,
        entry_point_source=lambda group: tuple(
            entry for entry in entries if entry.group == group
        ),
        host_version="1.0",
        **timeouts,
    )


def test_default_deadlines_fit_inside_electron_timeouts() -> None:
    registry = DesktopPluginRegistry(entry_point_source=lambda _group: ())

    # Electron allows 10 s for the sidecar start and 30 s for one request.
    assert registry._start_timeout == 6.0
    assert registry._call_timeout == 25.0
    assert registry._cleanup_timeout == 2.0


@pytest.mark.asyncio
async def test_slow_starts_run_concurrently_inside_one_budget(
    desktop_fixture_root: object,
) -> None:
    log: list[tuple[str, str, float]] = []
    providers: list[_TimedProvider] = []
    registry = _timed_registry(
        {
            "hang": {"start": "hang"},
            "slow-a": {"start": 0.2},
            "slow-b": {"start": 0.2},
            "slow-c": {"start": 0.2},
            "stubborn": {"start": "stubborn"},
        },
        log,
        providers,
        start_timeout_seconds=0.5,
    )
    registry.discover()
    loop = asyncio.get_running_loop()
    began = loop.time()

    await registry.start_all(lambda _plugin_id, _manifest: RecordingFacade())

    elapsed = loop.time() - began
    # One serial start of the three slow providers alone takes 0.6 s.
    assert 0.5 <= elapsed < 0.75
    states = {item.plugin_id: item for item in registry.plugins}
    for plugin_id in ("slow-a", "slow-b", "slow-c"):
        assert states[plugin_id].state is DesktopPluginState.STARTED
    for plugin_id in ("hang", "stubborn"):
        assert states[plugin_id].state is DesktopPluginState.FAILED
        error = states[plugin_id].error
        assert error is not None
        assert error.code is DesktopPluginErrorCode.START_FAILED
        assert error.message == "Desktop plugin start exceeded its time limit"
    assert all(
        provider.context is not None and provider.context.cancellation.cancelled
        for provider in providers
        if provider.plugin_id in {"hang", "stubborn"}
    )
    starts = sorted(at for event, _plugin_id, at in log if event == "start")
    assert len(starts) == 5
    assert starts[-1] - starts[0] < 0.1
    await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_cancelled_start_all_cancels_every_starting_provider(
    desktop_fixture_root: object,
) -> None:
    log: list[tuple[str, str, float]] = []
    providers: list[_TimedProvider] = []
    registry = _timed_registry(
        {"one": {"start": "hang"}, "two": {"start": "hang"}},
        log,
        providers,
        start_timeout_seconds=5,
    )
    registry.discover()
    starting = asyncio.create_task(
        registry.start_all(lambda _plugin_id, _manifest: RecordingFacade())
    )
    await asyncio.sleep(0.01)

    starting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await starting

    assert len(providers) == 2
    assert all(
        provider.context is not None and provider.context.cancellation.cancelled
        for provider in providers
    )
    await asyncio.sleep(0.01)
    assert not any(registry._runtimes[pid].provider_tasks for pid in ("one", "two"))


@pytest.mark.asyncio
async def test_four_slow_providers_all_stop_within_the_cleanup_bound(
    desktop_fixture_root: object,
) -> None:
    log: list[tuple[str, str, float]] = []
    providers: list[_TimedProvider] = []
    plugin_ids = ("p1", "p2", "p3", "p4")
    registry = _timed_registry(
        {plugin_id: {"stop": 0.2} for plugin_id in plugin_ids},
        log,
        providers,
        cleanup_timeout_seconds=0.5,
    )
    registry.discover()
    await registry.start_all(lambda _plugin_id, _manifest: RecordingFacade())
    tokens_at_first_stop: list[bool] = []
    original_stop = _TimedProvider.stop

    async def observe_tokens(self: _TimedProvider) -> None:
        if not tokens_at_first_stop:
            tokens_at_first_stop.extend(
                provider.context is not None and provider.context.cancellation.cancelled
                for provider in providers
            )
        await original_stop(self)

    for provider in providers:
        provider.stop = observe_tokens.__get__(provider)  # type: ignore[method-assign]
    loop = asyncio.get_running_loop()
    began = loop.time()

    await registry.stop_all()

    elapsed = loop.time() - began
    # Stopping the four providers one after another takes 0.8 s.
    assert elapsed < 0.5
    assert tokens_at_first_stop == [True, True, True, True]
    assert all(item.state is DesktopPluginState.STOPPED for item in registry.plugins)
    stops = [at for event, _plugin_id, at in log if event == "stop"]
    assert len(stops) == 4
    assert max(stops) - began < 0.1
    assert sorted(plugin_id for event, plugin_id, _at in log if event == "stopped") == [
        *plugin_ids
    ]


@pytest.mark.asyncio
async def test_hanging_providers_each_get_stop_and_share_one_deadline(
    desktop_fixture_root: object,
) -> None:
    log: list[tuple[str, str, float]] = []
    providers: list[_TimedProvider] = []
    registry = _timed_registry(
        {
            "h1": {"stop": "hang"},
            "h2": {"stop": "hang"},
            "h3": {"stop": "hang"},
            "h4": {"stop": "hang"},
            "quick": {},
        },
        log,
        providers,
        cleanup_timeout_seconds=0.2,
    )
    registry.discover()
    await registry.start_all(lambda _plugin_id, _manifest: RecordingFacade())
    loop = asyncio.get_running_loop()
    began = loop.time()

    await registry.stop_all()

    elapsed = loop.time() - began
    # One 0.2 s bound per hanging provider would take 0.8 s.
    assert 0.2 <= elapsed < 0.4
    assert sorted(plugin_id for event, plugin_id, _at in log if event == "stop") == [
        "h1",
        "h2",
        "h3",
        "h4",
        "quick",
    ]
    states = {item.plugin_id: item for item in registry.plugins}
    assert states["quick"].state is DesktopPluginState.STOPPED
    for plugin_id in ("h1", "h2", "h3", "h4"):
        error = states[plugin_id].error
        assert states[plugin_id].state is DesktopPluginState.FAILED
        assert error is not None
        assert error.code is DesktopPluginErrorCode.CLEANUP_TIMEOUT
    await asyncio.sleep(0.01)
    assert not any(runtime.provider_tasks for runtime in registry._runtimes.values())
