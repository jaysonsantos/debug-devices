"""Push the staged captures list and the webcam info to the page as SSE events, so the page does not poll them.

The push runs while a page listens (an open event stream). This process's own staged changes (a capture, its result,
a delete, a pop by this server's multimeter_read) push at once through the store listeners. A capture or a pop by
another MCP server process shows through the folder version within STAGED_PUSH_CHECK.
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

from debug_devices_mcp.staged import Capturer
from debug_devices_mcp.ui.constants import defaults
from debug_devices_mcp.ui.events import EventBus
from debug_devices_mcp.ui.staged_view import staged_list
from debug_devices_mcp.webcam_stream import StreamInfo

logger = logging.getLogger(__name__)

type StagedSource = Callable[[], Capturer | None]
type WebcamInfoSource = Callable[[], Awaitable[StreamInfo | None]]


def webcam_key(info: StreamInfo) -> StreamInfo:
    """What the page shows: the frame count only as "a frame came" (the count changes at each frame)."""
    return info.model_copy(update={"frames": min(info.frames, 1)})


class PagePush:
    """The two push loops of the page (staged captures, webcam info). The monitor starts them with the page."""

    def __init__(self, bus: EventBus, staged: StagedSource, webcam_info: WebcamInfoSource) -> None:
        self._bus = bus
        self._staged = staged
        self._webcam_info = webcam_info
        self._wake = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []

    def poke(self) -> None:
        """A staged change in this process: push the list now."""
        self._wake.set()

    def start(self) -> None:
        if not self._tasks:
            self._tasks = [
                asyncio.create_task(self._staged_loop(), name="page-push-staged"),
                asyncio.create_task(self._webcam_loop(), name="page-push-webcam"),
            ]

    async def stop(self) -> None:
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    # region: staged captures

    async def _wait_for_change(self) -> bool:
        """Wait for a poke or the next check. True: a poke (a change in this process)."""
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(defaults.STAGED_PUSH_CHECK.total_seconds()):
                await self._wake.wait()
        poked = self._wake.is_set()
        self._wake.clear()
        return poked

    async def _staged_loop(self) -> None:
        loop = asyncio.get_running_loop()
        version: object = None
        pushed: tuple[object, ...] | None = None
        listed_at = loop.time()
        while True:
            poked = await self._wait_for_change()
            capturer = self._staged()
            if capturer is None or not self._bus.has_subscribers:
                # A new page loads the list itself when its stream opens.
                version, pushed = None, None
                continue
            try:
                current = await capturer.store.version()
                sweep = bool(pushed) and loop.time() - listed_at >= defaults.STAGED_PUSH_SWEEP.total_seconds()
                if not (poked or sweep or current != version):
                    continue
                view = await staged_list(capturer)
            except OSError as exc:
                logger.warning("staged captures push: %s", exc)
                continue
            version, listed_at = current, loop.time()
            if view.page_key() != pushed:
                pushed = view.page_key()
                self._bus.publish_staged(view)

    # endregion: staged captures

    # region: webcam info

    async def _webcam_loop(self) -> None:
        pushed: StreamInfo | None = None
        while True:
            await asyncio.sleep(defaults.WEBCAM_PUSH_CHECK.total_seconds())
            if not self._bus.has_subscribers:
                pushed = None
                continue
            info = await self._webcam_info()
            if info is not None and webcam_key(info) != pushed:
                pushed = webcam_key(info)
                self._bus.publish_webcam(info)

    # endregion: webcam info
