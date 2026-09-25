"""Event bus for the monitor window: tool calls and phone state, fanned out to Server-Sent Events clients."""

import asyncio
import uuid
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, JsonValue

from debug_devices_mcp.focus import FocusReport, focus_report
from debug_devices_mcp.images import SnapshotOrientation
from debug_devices_mcp.phone_api import CameraStatus, OverlayArrow, OverlayBox
from debug_devices_mcp.ui.constants import defaults

MILLISECONDS_PER_SECOND = 1000

type Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


def to_ms(duration: timedelta) -> float:
    return duration.total_seconds() * MILLISECONDS_PER_SECOND


def from_ms(milliseconds: float) -> timedelta:
    return timedelta(milliseconds=milliseconds)


class CallSource(StrEnum):
    """Who started the tool call: the MCP client (the agent) or the monitor page."""

    MCP = "mcp"
    UI = "ui"


class CallStatus(StrEnum):
    RUNNING = "running"
    OK = "ok"
    ERROR = "error"


class EventKind(StrEnum):
    """The SSE `event:` name."""

    CALL = "call"
    PHONE = "phone"


# region: wire models


class ImageRef(BaseModel):
    index: int
    label: str


class ToolCallEvent(BaseModel):
    id: UUID
    tool: str
    source: CallSource
    arguments: dict[str, JsonValue]
    started_at: datetime
    duration_ms: float | None
    status: CallStatus
    summary: str
    error: str | None
    details: dict[str, JsonValue]
    images: list[ImageRef]
    # Another MCP server that sent this call to this monitor (for example "codex 1824788"). None: this server.
    origin: str | None = None


class PhoneState(BaseModel):
    serial: str | None = None
    status: CameraStatus | None = None
    scrcpy_running: bool = False
    has_snapshot: bool = False
    # Counts the phone snapshots. The page reloads the image (and resets its zoom) only when it changes.
    snapshot_seq: int = 0
    # The flips of the phone snapshots. The page shows the buttons and turns the live view the same way.
    orientation: SnapshotOrientation = SnapshotOrientation()
    # How far the phone is from the board and the detail it gives (from `status`, computed on the server).
    focus: FocusReport | None = None
    # The user's in-sensor zoom choice. The state of the phone is `status.in_sensor_zoom`.
    in_sensor_zoom_choice: bool = False
    # The user's autofocus mode choice: continuous or macro. The state of the phone is `status.af_mode`.
    af_mode_choice: str = "continuous"
    # The highlight boxes on the phone (phone_highlight): on the true-orientation snapshot, from 0 to 1.
    highlights: list[OverlayBox] = []
    # The arrows toward searched parts outside the view (phone_point_to): angles on the true-orientation snapshot.
    arrows: list[OverlayArrow] = []
    # The live tracking of the newest photo registration: off, following, or lost.
    tracking: str = "off"
    # The last time the board or the phone moved (the scene watcher). The page shows a short note.
    scene_changed_at: datetime | None = None
    # The phone screen stream in the page: off, starting, streaming, or error.
    screen: str = "off"
    screen_error: str | None = None


class BusMessage(BaseModel):
    kind: EventKind
    data: ToolCallEvent | PhoneState


# endregion: wire models


@dataclass
class AttachedImage:
    label: str
    jpeg: bytes


@dataclass
class ToolCall:
    """One tool call. The tool code adds images and details while it runs, through `current_call()`."""

    tool: str
    source: CallSource
    arguments: dict[str, JsonValue]
    started_at: datetime
    id: UUID = field(default_factory=uuid.uuid7)
    finished_at: datetime | None = None
    status: CallStatus = CallStatus.RUNNING
    summary: str = ""
    error: str | None = None
    details: dict[str, JsonValue] = field(default_factory=dict)
    images: list[AttachedImage] = field(default_factory=list)
    origin: str | None = None

    def attach_image(self, jpeg: bytes, label: str) -> None:
        self.images.append(AttachedImage(label=label, jpeg=jpeg))

    def set_detail(self, key: str, value: JsonValue) -> None:
        self.details[key] = value

    @property
    def duration(self) -> timedelta | None:
        return None if self.finished_at is None else self.finished_at - self.started_at

    def to_event(self) -> ToolCallEvent:
        duration = self.duration
        return ToolCallEvent(
            id=self.id,
            tool=self.tool,
            source=self.source,
            arguments=self.arguments,
            started_at=self.started_at,
            duration_ms=None if duration is None else to_ms(duration),
            status=self.status,
            summary=self.summary,
            error=self.error,
            details=self.details,
            images=[ImageRef(index=index, label=image.label) for index, image in enumerate(self.images)],
            origin=self.origin,
        )


_current_call: ContextVar[ToolCall | None] = ContextVar("current_call", default=None)


def current_call() -> ToolCall | None:
    """The tool call that runs in this task, or None outside a recorded call."""
    return _current_call.get()


def truncate(text: str, limit: int = defaults.SUMMARY_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def json_arguments(arguments: Mapping[str, Any]) -> dict[str, JsonValue]:
    """Keep JSON values as they are, and show anything else as text."""
    result: dict[str, JsonValue] = {}
    for key, value in arguments.items():
        result[key] = value if isinstance(value, str | int | float | bool | None | list | dict) else repr(value)
    return result


class EventBus:
    """Keeps the recent calls and sends every change to each subscriber queue."""

    def __init__(
        self,
        history_size: int = defaults.HISTORY_SIZE,
        image_history_size: int = defaults.IMAGE_HISTORY_SIZE,
        queue_size: int = defaults.SUBSCRIBER_QUEUE_SIZE,
        clock: Clock = utc_now,
    ) -> None:
        self._calls: deque[ToolCall] = deque(maxlen=history_size)
        self._image_history_size = image_history_size
        self._queue_size = queue_size
        self._clock = clock
        self._subscribers: set[asyncio.Queue[BusMessage]] = set()
        self.phone = PhoneState()
        # Calls that other MCP servers sent to this monitor, with the same limits per server.
        self._remote: dict[str, deque[ToolCall]] = {}
        self._history_size = history_size

    # region: calls

    @asynccontextmanager
    async def record(
        self, tool: str, arguments: Mapping[str, Any], source: CallSource = CallSource.MCP
    ) -> AsyncIterator[ToolCall]:
        """Record a tool call: publish it at the start and at the end. An exception marks it as an error."""
        call = ToolCall(tool=tool, source=source, arguments=json_arguments(arguments), started_at=self._clock())
        self._add(call)
        token = _current_call.set(call)
        try:
            yield call
        except BaseException as exc:
            call.status = CallStatus.ERROR
            call.error = truncate(str(exc) or type(exc).__name__)
            raise
        else:
            if call.status == CallStatus.RUNNING:
                call.status = CallStatus.OK
        finally:
            _current_call.reset(token)
            call.finished_at = self._clock()
            self.publish_call(call)

    def _add(self, call: ToolCall) -> None:
        self._calls.append(call)
        for old in list(self._calls)[: -self._image_history_size]:
            old.images.clear()
        self.publish_call(call)

    def calls(self) -> list[ToolCall]:
        """The calls of this server and of the other servers, oldest first."""
        if not self._remote:
            return list(self._calls)
        remote = (call for calls in self._remote.values() for call in calls)
        return sorted([*self._calls, *remote], key=lambda call: call.started_at)

    def find_call(self, call_id: UUID) -> ToolCall | None:
        return next((call for call in self.calls() if call.id == call_id), None)

    # endregion: calls

    # region: calls of other servers

    def ingest(self, event: ToolCallEvent, origin: str) -> ToolCall:
        """Add or update a call that another MCP server sent. A late start event never undoes the end."""
        calls = self._remote.get(origin)
        if calls is None:
            if len(self._remote) >= defaults.MAX_REMOTE_SERVERS:
                del self._remote[next(iter(self._remote))]
            calls = self._remote[origin] = deque(maxlen=self._history_size)
        call = next((known for known in calls if known.id == event.id), None)
        if call is None:
            call = ToolCall(
                tool=event.tool,
                source=event.source,
                arguments=event.arguments,
                started_at=event.started_at,
                id=event.id,
                origin=origin,
            )
            calls.append(call)
            for old in list(calls)[: -self._image_history_size]:
                old.images.clear()
        elif event.status is CallStatus.RUNNING and call.status is not CallStatus.RUNNING:
            return call
        call.status = event.status
        call.summary = event.summary
        call.error = event.error
        call.details = event.details
        if event.duration_ms is not None:
            call.finished_at = event.started_at + from_ms(event.duration_ms)
        self.publish_call(call)
        return call

    def attach_ingested_image(self, origin: str, call_id: UUID, label: str, data: bytes) -> bool:
        call = next((known for known in self._remote.get(origin, ()) if known.id == call_id), None)
        if call is None:
            return False
        call.attach_image(data, label)
        self.publish_call(call)
        return True

    # endregion: calls of other servers

    # region: publish

    def publish_call(self, call: ToolCall) -> None:
        self._publish(BusMessage(kind=EventKind.CALL, data=call.to_event()))

    def update_phone(self, **changes: Any) -> None:
        status = changes.get("status")
        if isinstance(status, CameraStatus):
            changes["focus"] = focus_report(status)
        self.phone = self.phone.model_copy(update=changes)
        self._publish(BusMessage(kind=EventKind.PHONE, data=self.phone))

    def _publish(self, message: BusMessage) -> None:
        for queue in self._subscribers:
            # A slow page misses updates; it reloads the state on reconnect.
            with suppress(asyncio.QueueFull):
                queue.put_nowait(message)

    @contextmanager
    def subscribe(self) -> Iterator[asyncio.Queue[BusMessage]]:
        queue: asyncio.Queue[BusMessage] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.add(queue)
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)

    # endregion: publish
