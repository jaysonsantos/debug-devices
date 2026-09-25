from datetime import UTC, datetime, timedelta

import pytest

from debug_devices_mcp.ui.events import (
    BusMessage,
    CallSource,
    CallStatus,
    EventBus,
    EventKind,
    ToolCallEvent,
    current_call,
    truncate,
)


class StepClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        value = self.now
        self.now += timedelta(milliseconds=250)
        return value


def call_events(messages: list[BusMessage]) -> list[ToolCallEvent]:
    return [message.data for message in messages if isinstance(message.data, ToolCallEvent)]


def drain(queue) -> list[BusMessage]:
    messages = []
    while not queue.empty():
        messages.append(queue.get_nowait())
    return messages


async def test_record_publishes_start_and_end_with_duration() -> None:
    bus = EventBus(clock=StepClock())
    with bus.subscribe() as queue:
        async with bus.record("phone_status", {"x": 1}) as call:
            assert current_call() is call
            call.attach_image(b"jpeg", "frame")
            call.set_detail("reading", {"value": 1.5})
        assert current_call() is None
        events = call_events(drain(queue))
    assert [event.status for event in events] == [CallStatus.RUNNING, CallStatus.OK]
    end = events[-1]
    assert end.duration_ms == 250
    assert end.arguments == {"x": 1}
    assert end.source == CallSource.MCP
    assert end.details == {"reading": {"value": 1.5}}
    assert [image.label for image in end.images] == ["frame"]


async def test_record_marks_errors_and_reraises() -> None:
    bus = EventBus()
    with pytest.raises(ValueError, match="broken"):
        async with bus.record("webcam_snapshot", {}, CallSource.UI):
            raise ValueError("broken")
    [call] = bus.calls()
    assert call.status == CallStatus.ERROR
    assert call.error == "broken"
    assert call.source == CallSource.UI


async def test_history_is_bounded_and_old_images_are_dropped() -> None:
    bus = EventBus(history_size=3, image_history_size=1)
    for index in range(4):
        async with bus.record(f"tool{index}", {}) as call:
            call.attach_image(b"jpeg", "frame")
    calls = bus.calls()
    assert [call.tool for call in calls] == ["tool1", "tool2", "tool3"]
    assert [len(call.images) for call in calls] == [0, 0, 1]
    assert bus.find_call(calls[-1].id) is calls[-1]


async def test_update_phone_publishes_state() -> None:
    bus = EventBus()
    with bus.subscribe() as queue:
        bus.update_phone(serial="0a1b2c3d")
        [message] = drain(queue)
    assert message.kind == EventKind.PHONE
    assert bus.phone.serial == "0a1b2c3d"


async def test_full_subscriber_queue_drops_messages() -> None:
    bus = EventBus(queue_size=1)
    with bus.subscribe() as queue:
        bus.update_phone(serial="a")
        bus.update_phone(serial="b")
        assert queue.qsize() == 1


async def test_non_json_arguments_are_shown_as_text() -> None:
    bus = EventBus()
    async with bus.record("tool", {"path": object()}):
        pass
    assert isinstance(bus.calls()[0].arguments["path"], str)


def test_truncate() -> None:
    assert truncate("abc", 5) == "abc"
    assert truncate("abcdef", 5) == "abcd…"
