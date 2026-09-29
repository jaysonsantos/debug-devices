"""The page gets the staged captures list and the webcam info as SSE events (ui/page_push.py), not by polling: a change
by this process at once, a capture or a pop by another MCP server process through the folder version. Fakes only."""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid7

import pytest

from debug_devices_mcp.staged import StagedCapture, StagedState, StagedStore
from debug_devices_mcp.ui.constants import defaults
from debug_devices_mcp.ui.events import BusMessage, EventBus, EventKind
from debug_devices_mcp.ui.page_push import PagePush
from debug_devices_mcp.ui.staged_view import StagedList
from debug_devices_mcp.webcam_stream import StreamInfo

from .test_staged_store import PHOTO

CHECK = timedelta(milliseconds=20)
WAIT_SECONDS = 3


@dataclass
class StoreOnly:
    """The part of the capturer that the push reads."""

    store: StagedStore

    async def capture(self) -> StagedCapture:
        raise NotImplementedError


class WebcamInfo:
    def __init__(self) -> None:
        self.info = StreamInfo(device="/dev/video0", running=True, width=640, height=480, frames=0, error=None)
        self.calls = 0
        self.called = asyncio.Event()

    async def __call__(self) -> StreamInfo:
        self.calls += 1
        self.called.set()
        return self.info

    async def more_calls(self, count: int) -> None:
        for _ in range(count):
            self.called.clear()
            async with asyncio.timeout(WAIT_SECONDS):
                await self.called.wait()


@pytest.fixture(autouse=True)
def fast_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(defaults, "STAGED_PUSH_CHECK", CHECK)
    monkeypatch.setattr(defaults, "WEBCAM_PUSH_CHECK", CHECK)


async def next_of(queue: asyncio.Queue[BusMessage], kind: EventKind) -> BusMessage:
    async with asyncio.timeout(WAIT_SECONDS):
        while True:
            message = await queue.get()
            if message.kind is kind:
                return message


def pending_capture() -> StagedCapture:
    return StagedCapture(capture_id=str(uuid7()), captured_at=datetime.now(UTC), origin="other 2")


async def test_a_capture_and_a_pop_of_another_server_reach_the_page(tmp_path: Path) -> None:
    bus = EventBus()
    page_store = StagedStore(tmp_path)
    push = PagePush(bus, lambda: StoreOnly(page_store), WebcamInfo())
    # Another MCP server process: the same folder, but no listener of this process.
    other = StagedStore(tmp_path)
    with bus.subscribe() as queue:
        push.start()
        try:
            capture = pending_capture()
            await other.add(capture)
            added = (await next_of(queue, EventKind.STAGED)).data
            assert isinstance(added, StagedList)
            assert [(view.capture_id, view.state) for view in added.captures] == [
                (capture.capture_id, StagedState.PENDING)
            ]
            await other.finish(capture.model_copy(update={"state": StagedState.READY, "photo": PHOTO}), b"photo", [])
            ready = (await next_of(queue, EventKind.STAGED)).data
            assert isinstance(ready, StagedList)
            assert [(view.state, view.has_photo) for view in ready.captures] == [(StagedState.READY, True)]
            # The agent's multimeter_read in the other server pops it: the page list empties.
            assert len(await other.pop_all()) == 1
            popped = (await next_of(queue, EventKind.STAGED)).data
            assert isinstance(popped, StagedList)
            assert popped.captures == []
        finally:
            await push.stop()


async def test_a_change_of_this_process_pushes_at_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A long folder check: only the store listener can push in time.
    monkeypatch.setattr(defaults, "STAGED_PUSH_CHECK", timedelta(seconds=30))
    bus = EventBus()
    store = StagedStore(tmp_path)
    push = PagePush(bus, lambda: StoreOnly(store), WebcamInfo())
    store.listeners.append(push.poke)
    with bus.subscribe() as queue:
        push.start()
        try:
            await store.add(pending_capture())
            listed = (await next_of(queue, EventKind.STAGED)).data
            assert isinstance(listed, StagedList)
            assert len(listed.captures) == 1
        finally:
            await push.stop()


async def test_the_webcam_info_is_pushed_only_when_the_page_view_changes(tmp_path: Path) -> None:
    bus = EventBus()
    webcam = WebcamInfo()
    push = PagePush(bus, lambda: None, webcam)
    with bus.subscribe() as queue:
        push.start()
        try:
            first = (await next_of(queue, EventKind.WEBCAM)).data
            assert isinstance(first, StreamInfo)
            assert first.frames == 0
            webcam.info = webcam.info.model_copy(update={"frames": 1})
            assert (await next_of(queue, EventKind.WEBCAM)).data.frames == 1
            # More frames only: the page shows the same, so no event.
            webcam.info = webcam.info.model_copy(update={"frames": 50})
            await webcam.more_calls(3)
            assert all(message.kind is not EventKind.WEBCAM for message in drain(queue))
            webcam.info = webcam.info.model_copy(update={"error": "ffmpeg stopped"})
            assert (await next_of(queue, EventKind.WEBCAM)).data.error == "ffmpeg stopped"
        finally:
            await push.stop()


async def test_no_page_no_work(tmp_path: Path) -> None:
    webcam = WebcamInfo()
    push = PagePush(EventBus(), lambda: StoreOnly(StagedStore(tmp_path)), webcam)
    push.start()
    await asyncio.sleep(CHECK.total_seconds() * 5)
    await push.stop()
    # Without an open event stream, the push reads nothing (no webcam info, no staged folder).
    assert webcam.calls == 0
    assert not tmp_path.joinpath(".lock").exists()


def drain(queue: asyncio.Queue[BusMessage]) -> list[BusMessage]:
    messages = []
    while not queue.empty():
        messages.append(queue.get_nowait())
    return messages
