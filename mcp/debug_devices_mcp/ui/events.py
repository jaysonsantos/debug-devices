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

from debug_devices_mcp.phone_api import CameraStatus
from debug_devices_mcp.ui.constants import defaults

MILLISECONDS_PER_SECOND = 1000

type Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


def to_ms(duration: timedelta) -> float:
    return duration.total_seconds() * MILLISECONDS_PER_SECOND


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


class PhoneState(BaseModel):
    serial: str | None = None
    status: CameraStatus | None = None
    scrcpy_running: bool = False
    has_snapshot: bool = False
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
        return list(self._calls)

    def find_call(self, call_id: UUID) -> ToolCall | None:
        return next((call for call in self._calls if call.id == call_id), None)

    # endregion: calls

    # region: publish

    def publish_call(self, call: ToolCall) -> None:
        self._publish(BusMessage(kind=EventKind.CALL, data=call.to_event()))

    def update_phone(self, **changes: Any) -> None:
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
