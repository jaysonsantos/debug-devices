"""The monitor: records every MCP tool call, owns the webcam stream and scrcpy, and serves the page."""

import asyncio
import base64
import binascii
import contextlib
import logging
import os
import socket
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, ImageContent, TextContent
from pydantic import BaseModel, ValidationError

from debug_devices_mcp.phone_api import CameraStatus, PhoneError
from debug_devices_mcp.phone_screen import PhoneScreen, ScreenState
from debug_devices_mcp.remote_webcam import MonitorIdentity, SharedWebcam
from debug_devices_mcp.scrcpy import ScrcpyError, ScrcpyLauncher
from debug_devices_mcp.ui.app import create_app
from debug_devices_mcp.ui.constants import APP_NAME, defaults, details, http, labels, tools
from debug_devices_mcp.ui.desktop import BrowserOpener
from debug_devices_mcp.ui.events import CallSource, CallStatus, EventBus, ToolCall, current_call, truncate
from debug_devices_mcp.ui.settings import EffectiveSettings, SettingsStore, UiSettings
from debug_devices_mcp.webcam import Crop
from debug_devices_mcp.webcam_stream import FrameSource, WebcamStream

logger = logging.getLogger(__name__)

STARTUP_POLL_SECONDS = 0.05
STARTUP_TIMEOUT_SECONDS = 10
GRACEFUL_SHUTDOWN_SECONDS = 1
# How often the monitor reads the phone status after phone_connect (no tool call, no log row).
STATUS_POLL_SECONDS = 1
# The whole cleanup at exit: web server, webcam stream, scrcpy, phone screen (adb forward --remove).
STOP_TIMEOUT_SECONDS = 15
TEXT_SEPARATOR = "\n"
PHONE_STATUS_TOOLS = frozenset({tools.PHONE_STATUS, tools.PHONE_ZOOM, tools.PHONE_TORCH, tools.PHONE_ROTATION})

type ToolCaller = Callable[..., Awaitable[Any]]
type SettingsListener = Callable[[EffectiveSettings], None]
type StatusReader = Callable[[], Awaitable[CameraStatus]]


class ConnectedPhone(BaseModel):
    """The part of the `phone_connect` result that the monitor reads."""

    serial: str
    status: CameraStatus


class MonitorOptions(BaseModel):
    host: str = defaults.HOST
    port: int = defaults.PORT
    open_browser: bool = True


@dataclass
class MonitorParts:
    """The processes that the monitor owns. None turns a part off."""

    stream: WebcamStream | None = None
    scrcpy: ScrcpyLauncher | None = None
    opener: BrowserOpener | None = None
    # Reads the frames of another monitor when that monitor owns the webcam.
    shared: SharedWebcam | None = None
    # The phone screen in the page (scrcpy server stream).
    screen: PhoneScreen | None = None
    # Reads the camera status without a tool call. The page turns the phone screen with `rotation_degrees`.
    status_reader: StatusReader | None = None


class _EmbeddedServer(uvicorn.Server):
    """uvicorn inside the MCP process. The MCP server keeps its own signal handlers."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


def bind_socket(host: str, port: int) -> socket.socket:
    """Bind `port` on `host`. When the port is busy, take a free one."""
    for candidate in (port, defaults.ANY_PORT):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, candidate))
        except OSError as exc:
            sock.close()
            logger.info("monitor port %s on %s is busy: %s", candidate, host, exc)
            continue
        return sock
    raise OSError(f"cannot bind a port on {host}")


class RecordingFrameSource:
    """Adds every frame that a tool takes to the running tool call, so the page shows what the model saw."""

    def __init__(self, inner: FrameSource) -> None:
        self.inner = inner

    @property
    def device(self) -> Path:
        return self.inner.device

    @property
    def crop(self) -> Crop | None:
        return self.inner.crop

    async def capture_jpeg(self) -> bytes:
        jpeg = await self.inner.capture_jpeg()
        call = current_call()
        if call is not None:
            label = labels.MODEL_INPUT if call.tool == tools.MULTIMETER_READ else labels.WEBCAM_FRAME
            call.attach_image(jpeg, label)
        return jpeg


class Monitor:
    def __init__(
        self,
        start_settings: EffectiveSettings,
        store: SettingsStore,
        options: MonitorOptions | None = None,
        parts: MonitorParts | None = None,
    ) -> None:
        parts = parts or MonitorParts()
        self.bus = EventBus()
        self.options = options or MonitorOptions()
        self.stream = parts.stream
        self.scrcpy = parts.scrcpy
        self.shared = parts.shared
        self.screen = parts.screen
        self.status_reader = parts.status_reader
        self._status_task: asyncio.Task[None] | None = None
        # The URL of the other monitor that owns the webcam, when there is one.
        self.webcam_owner: str | None = None
        self._opener = parts.opener or BrowserOpener()
        self._store = store
        self._start_settings = start_settings
        self.saved = store.load()
        self.effective = start_settings.with_saved(self.saved)
        self._listeners: list[SettingsListener] = []
        self._call_tool: ToolCaller | None = None
        self._server: _EmbeddedServer | None = None
        self._serve_task: asyncio.Task[None] | None = None
        self.closing = asyncio.Event()
        self.url: str | None = None
        self.last_snapshot: bytes | None = None

    # region: tool calls

    def instrument(self, server: MCPServer) -> None:
        """Record every tool call of `server`, from the MCP client and from the page."""
        self._call_tool = server.call_tool

        async def call_tool(name: str, arguments: dict[str, Any], context: Any = None) -> Any:
            _, result = await self._record_call(name, arguments, CallSource.MCP, context)
            return result

        server.call_tool = call_tool  # type: ignore[method-assign]

    async def call_from_ui(self, name: str, arguments: dict[str, Any]) -> tuple[ToolCall, CallToolResult]:
        """Run a tool for the page. A tool failure raises `ToolError`; the log has the call."""
        return await self._record_call(name, arguments, CallSource.UI)

    async def _record_call(
        self, name: str, arguments: dict[str, Any], source: CallSource, context: Any = None
    ) -> tuple[ToolCall, Any]:
        if self._call_tool is None:
            raise RuntimeError("the monitor is not attached to an MCP server")
        async with self.bus.record(name, arguments, source) as call:
            result = await self._call_tool(name, arguments, context)
            await self._after_call(call, result)
        return call, result

    def frame_source(self, inner: FrameSource) -> FrameSource:
        return RecordingFrameSource(inner)

    async def _after_call(self, call: ToolCall, result: Any) -> None:
        if not isinstance(result, CallToolResult):
            return
        texts = [block.text for block in result.content if isinstance(block, TextContent)]
        call.summary = truncate(TEXT_SEPARATOR.join(texts))
        if result.is_error:
            call.status = CallStatus.ERROR
            call.error = call.summary
            return
        result_images = [image for block in result.content if (image := _image_bytes(block)) is not None]
        if not call.images:
            # The image in a multimeter_read result is the image that went to the model.
            label = labels.MODEL_INPUT if call.tool == tools.MULTIMETER_READ else labels.RESULT_IMAGE
            for image in result_images:
                call.attach_image(image, label)
        structured = result.structured_content
        if call.tool == tools.MULTIMETER_READ and structured is not None:
            call.set_detail(details.READING, structured)
        elif call.tool == tools.PHONE_CONNECT and structured is not None:
            await self._phone_connected(call, structured)
        elif call.tool in PHONE_STATUS_TOOLS and structured is not None:
            self._phone_status(structured)
        elif call.tool == tools.PHONE_SNAPSHOT and result_images:
            self.last_snapshot = result_images[0]
            self.bus.update_phone(has_snapshot=True)

    def _phone_status(self, structured: dict[str, Any]) -> None:
        try:
            self.bus.update_phone(status=CameraStatus.model_validate(structured))
        except ValidationError as exc:
            logger.warning("unexpected camera status: %s", exc)

    async def _phone_connected(self, call: ToolCall, structured: dict[str, Any]) -> None:
        try:
            connected = ConnectedPhone.model_validate(structured)
        except ValidationError as exc:
            logger.warning("unexpected phone_connect result: %s", exc)
            return
        self.bus.update_phone(serial=connected.serial, status=connected.status)
        self._start_status_poll()
        if self.screen is not None:
            started = self.screen.ensure_running(connected.serial)
            call.set_detail(details.SCREEN, "started" if started else "already running")
        if self.scrcpy is None:
            return
        try:
            started = await self.scrcpy.ensure_running(connected.serial)
            call.set_detail(details.SCRCPY, "started" if started else "already running")
        except ScrcpyError as exc:
            call.set_detail(details.SCRCPY, str(exc))
        self.bus.update_phone(scrcpy_running=self.scrcpy.running)

    # endregion: tool calls

    # region: settings

    def add_settings_listener(self, listener: SettingsListener) -> None:
        """Call `listener` now and after each change."""
        self._listeners.append(listener)
        listener(self.effective)

    def crop(self) -> Crop | None:
        return self.effective.webcam_crop

    def update_settings(self, saved: UiSettings) -> EffectiveSettings:
        old = self.effective
        self._store.save(saved)
        self.saved = saved
        self.effective = self._start_settings.with_saved(saved)
        if self.stream is not None and old.webcam_warmup_frames != self.effective.webcam_warmup_frames:
            self.stream.set_warmup_frames(self.effective.webcam_warmup_frames)
            self.stream.restart()
        for listener in self._listeners:
            listener(self.effective)
        return self.effective

    @property
    def settings_path(self) -> Path:
        return self._store.path

    @property
    def start_settings(self) -> EffectiveSettings:
        return self._start_settings

    # endregion: settings

    # region: lifecycle

    async def start(self) -> None:
        """Serve the page. If another monitor owns the webcam, use its frames and do not open a browser."""
        if self.shared is not None and await self.shared.use_remote_if_present():
            assert self.shared.remote_identity is not None
            self.webcam_owner = self.shared.remote_identity.url
        elif self.stream is not None:
            self.start_stream()
        sock = bind_socket(self.options.host, self.options.port)
        host, port = sock.getsockname()[:2]
        self.url = f"{http.SCHEME}://{host}:{port}/"
        config = uvicorn.Config(
            create_app(self),
            log_level=logging.WARNING,
            lifespan="off",
            timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
        )
        self._server = _EmbeddedServer(config)
        self._serve_task = asyncio.create_task(self._server.serve(sockets=[sock]), name="monitor-http")
        async with asyncio.timeout(STARTUP_TIMEOUT_SECONDS):
            while not self._server.started:
                if self._serve_task.done():
                    await self._serve_task
                    raise RuntimeError("the monitor web server stopped at start")
                await asyncio.sleep(STARTUP_POLL_SECONDS)
        logger.warning("monitor window: %s", self.url)
        if self.options.open_browser and self.webcam_owner is None:
            await self._opener.open(self.url)

    def _start_status_poll(self) -> None:
        if self.status_reader is None or (self._status_task is not None and not self._status_task.done()):
            return
        self._status_task = asyncio.create_task(self._poll_status(self.status_reader), name="phone-status")

    async def _poll_status(self, read: StatusReader) -> None:
        """Follow the phone: the page needs the new `rotation_degrees` when the phone turns."""
        while True:
            await asyncio.sleep(STATUS_POLL_SECONDS)
            try:
                status = await read()
            except PhoneError:
                continue
            if status != self.bus.phone.status:
                self.bus.update_phone(status=status)

    def screen_changed(self, state: ScreenState) -> None:
        """Listener for `PhoneScreen`: show the stream state on the page."""
        self.bus.update_phone(screen=state.status, screen_error=state.error)

    def start_stream(self) -> None:
        """Own the webcam: start the local stream. Also called when the other monitor stops."""
        self.webcam_owner = None
        if self.stream is not None:
            self.stream.set_warmup_frames(self.effective.webcam_warmup_frames)
            self.stream.start()

    def identity(self) -> MonitorIdentity:
        info = self.stream.info() if self.stream is not None else None
        return MonitorIdentity(
            app=APP_NAME,
            pid=os.getpid(),
            url=self.url or "",
            webcam=info.device if info is not None else None,
            webcam_running=info is not None and info.running and self.webcam_owner is None,
            webcam_crop=self.effective.webcam_crop,
        )

    async def stop(self) -> None:
        """Stop everything. The MCP shutdown cancels the lifespan, so the cleanup runs shielded, with a limit."""
        with anyio.move_on_after(STOP_TIMEOUT_SECONDS, shield=True):
            await self._stop()

    async def _stop(self) -> None:
        self.closing.set()
        if self._server is not None:
            self._server.should_exit = True
        if self._serve_task is not None:
            with contextlib.suppress(Exception):
                await self._serve_task
        if self.stream is not None:
            await self.stream.stop()
        if self.scrcpy is not None:
            await self.scrcpy.stop()
        if self.screen is not None:
            await self.screen.stop()
        if self._status_task is not None:
            self._status_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._status_task
        if self.shared is not None:
            await self.shared.remote.aclose()

    # endregion: lifecycle


def _image_bytes(block: object) -> bytes | None:
    if not isinstance(block, ImageContent):
        return None
    try:
        return base64.b64decode(block.data)
    except binascii.Error, ValueError:
        return None
