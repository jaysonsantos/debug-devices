"""The monitor: records every MCP tool call, owns the webcam stream and scrcpy, and serves the page."""

import asyncio
import base64
import binascii
import contextlib
import logging
import os
import socket
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import anyio
import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, ImageContent, TextContent
from pydantic import BaseModel, ValidationError

from debug_devices_mcp.board.tools import BoardSummary
from debug_devices_mcp.camera_choice import AfModeChoice, InSensorZoomChoice
from debug_devices_mcp.constants import images
from debug_devices_mcp.images import SnapshotOrientation, downscale_jpeg, orient_jpeg
from debug_devices_mcp.orientation import OrientationState
from debug_devices_mcp.phone_api import CameraStatus, OverlayArrow, OverlayBox, PhoneError
from debug_devices_mcp.phone_screen import PhoneScreen, ScreenState
from debug_devices_mcp.remote_webcam import MonitorIdentity, SharedWebcam
from debug_devices_mcp.scene import SceneWatcher
from debug_devices_mcp.scrcpy import ScrcpyError, ScrcpyLauncher
from debug_devices_mcp.screen_mjpeg import ScreenTranscoder
from debug_devices_mcp.ui.app import create_app
from debug_devices_mcp.ui.board import BoardPanel, RemoteBoard
from debug_devices_mcp.ui.constants import APP_NAME, UiStart, defaults, details, http, labels, tools
from debug_devices_mcp.ui.desktop import BrowserOpener
from debug_devices_mcp.ui.events import CallSource, CallStatus, EventBus, ToolCall, current_call, truncate
from debug_devices_mcp.ui.forward import CallForwarder, origin_label, remove_token, token_dir, write_token
from debug_devices_mcp.ui.settings import EffectiveSettings, SettingsStore, UiSettings
from debug_devices_mcp.ui.version import code_version
from debug_devices_mcp.webcam import Crop
from debug_devices_mcp.webcam_stream import FrameSource, WebcamStream, crop_jpeg

logger = logging.getLogger(__name__)

STARTUP_POLL_SECONDS = 0.05
STARTUP_TIMEOUT_SECONDS = 10
GRACEFUL_SHUTDOWN_SECONDS = 1
# How often the monitor reads the phone status after phone_connect (no tool call, no log row).
STATUS_POLL_SECONDS = 1
# The whole cleanup at exit: web server, webcam stream, scrcpy, phone screen (adb forward --remove).
STOP_TIMEOUT_SECONDS = 15
TEXT_SEPARATOR = "\n"
# PhoneState.tracking while the live tracker follows the board (a move is then no scene change for the page).
TRACKING_FOLLOWING = "following"
BOARD_PATH_ARGUMENT = "path"
PHONE_STATUS_TOOLS = frozenset(
    {
        tools.PHONE_STATUS,
        tools.PHONE_ZOOM,
        tools.PHONE_TORCH,
        tools.PHONE_ROTATION,
        tools.PHONE_IN_SENSOR_ZOOM,
        tools.PHONE_FOCUS,
        tools.PHONE_AF_MODE,
    }
)

type ToolCaller = Callable[..., Awaitable[Any]]
type SettingsListener = Callable[[EffectiveSettings], None]
type StatusReader = Callable[[], Awaitable[CameraStatus]]
# Removes the adb forward of the camera API for this serial (bench_stop).
type ForwardRemover = Callable[[str], Awaitable[None]]
type Clock = Callable[[], float]
type SnapshotReader = Callable[[], Awaitable[bytes]]


NO_CROP_TEXT = "none (the whole frame)"


def crop_text(crop: Crop | None) -> str:
    return NO_CROP_TEXT if crop is None else f"{crop.x},{crop.y},{crop.width},{crop.height}"


def crop_preview(jpeg: bytes, crop: Crop | None) -> bytes:
    """The area that multimeter_read sends, scaled down for the log."""
    area = jpeg if crop is None else crop_jpeg(jpeg, crop)
    return downscale_jpeg(area, defaults.CROP_PREVIEW_MAX_SIDE).data


class ConnectedPhone(BaseModel):
    """The part of the `phone_connect` result that the monitor reads."""

    serial: str
    status: CameraStatus


class MonitorOptions(BaseModel):
    host: str = defaults.HOST
    port: int = defaults.PORT
    # Off unless the settings turn it on, so a test never opens a browser window.
    open_browser: bool = False
    # The settings default is lazy. Eager here keeps the page and the stream at `start()`, like a dev monitor.
    start: UiStart = UiStart.EAGER
    # Lazy mode only. Zero keeps the stream on.
    webcam_idle_timeout: timedelta = defaults.WEBCAM_IDLE_TIMEOUT


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
    forward_remover: ForwardRemover | None = None
    # The flips of the phone snapshots (shared with the tools). The page shows the snapshot in this orientation.
    orientation: OrientationState | None = None
    # The in-sensor zoom choice (shared with the tools).
    in_sensor_zoom: InSensorZoomChoice | None = None
    # The autofocus mode choice (shared with the tools).
    af_mode: AfModeChoice | None = None
    clock: Clock = field(default=time.monotonic)


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


class OnDemandFrameSource:
    """Starts the webcam on the first frame request, and counts the frame users for the idle release."""

    def __init__(self, inner: FrameSource, monitor: Monitor) -> None:
        self.inner = inner
        self._monitor = monitor

    @property
    def device(self) -> Path:
        return self.inner.device

    @property
    def crop(self) -> Crop | None:
        return self.inner.crop

    async def capture_jpeg(self) -> bytes:
        async with self._monitor.webcam_user():
            return await self.inner.capture_jpeg()


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
        self.orientation = parts.orientation
        self.in_sensor_zoom = parts.in_sensor_zoom
        self.af_mode = parts.af_mode
        # Rendered page images of the raw snapshot: (snapshot number, orientation, full size) -> JPEG.
        self._rendered: dict[tuple[int, SnapshotOrientation, bool], bytes] = {}
        self._forward_remover = parts.forward_remover
        self._clock = parts.clock
        self._page_lock = asyncio.Lock()
        self._webcam_lock = asyncio.Lock()
        self._webcam_users = 0
        self._webcam_viewers = 0
        self._last_webcam_use = self._clock()
        self._idle_task: asyncio.Task[None] | None = None
        self._page_stop_task: asyncio.Task[None] | None = None
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
        # The page of another MCP server on the configured port (the primary). Then this server serves no page and
        # sends its calls there.
        self.primary_url: str | None = None
        # The phone screen as MJPEG for browsers without H.264 in WebCodecs (set by setup with the phone screen).
        self.screen_mjpeg: ScreenTranscoder | None = None
        # Watches the phone screen for a moved board or phone while a phone session runs (setup sets it).
        self.scene_watcher: SceneWatcher | None = None
        # The Board panel of the page (setup sets it, with the MCP server's board session).
        self.board_panel: BoardPanel | None = None
        # Changes with each server start and each change of the page files: an open page reloads itself.
        self.code_version = code_version()
        self.client_name: str | None = None
        self.forwarder: CallForwarder | None = None
        self.token_dir: Path = token_dir()
        self.ingest_token: str | None = None
        self._token_port: int | None = None
        # The raw still from the phone (true orientation, full size). The page gets it through `render_snapshot`.
        self.last_snapshot_raw: bytes | None = None
        # Full phone snapshots by tool call, until the call ends (the tool result has only the scaled image).
        self._full_snapshots: dict[UUID, bytes] = {}

    # region: tool calls

    def instrument(self, server: MCPServer) -> None:
        """Record every tool call of `server`, from the MCP client and from the page."""
        self._call_tool = server.call_tool

        async def call_tool(name: str, arguments: dict[str, Any], context: Any = None) -> Any:
            self._remember_client(context)
            _, result = await self._record_call(name, arguments, CallSource.MCP, context)
            return result

        server.call_tool = call_tool  # type: ignore[method-assign]

    def _remember_client(self, context: Any) -> None:
        """The client name from initialize (for example "codex"), for the origin label on the primary page."""
        if self.client_name is not None or context is None:
            return
        with contextlib.suppress(AttributeError, LookupError, ValueError, RuntimeError):
            self.client_name = context.session.client_params.client_info.name

    def origin(self) -> str:
        return origin_label(self.client_name)

    @property
    def page_url(self) -> str | None:
        """The page that the user opens: this server's page, or the page of the primary."""
        return self.url or self.primary_url

    def is_secondary(self) -> bool:
        return self._server is None and self.primary_url is not None

    async def call_from_ui(self, name: str, arguments: dict[str, Any]) -> tuple[ToolCall, CallToolResult]:
        """Run a tool for the page. A tool failure raises `ToolError`; the log has the call."""
        return await self._record_call(name, arguments, CallSource.UI)

    async def _record_call(
        self, name: str, arguments: dict[str, Any], source: CallSource, context: Any = None
    ) -> tuple[ToolCall, Any]:
        if self._call_tool is None:
            raise RuntimeError("the monitor is not attached to an MCP server")
        if source is CallSource.MCP:
            # monitor_open and bench_start open the browser themselves, when the caller asks.
            await self._ensure_page_quietly(auto_open=name not in {tools.MONITOR_OPEN, tools.BENCH_START})
        async with self.bus.record(name, arguments, source) as call:
            try:
                result = await self._call_tool(name, arguments, context)
                await self._after_call(call, result)
            finally:
                self._full_snapshots.pop(call.id, None)
        return call, result

    def af_mode_changed(self, mode: str) -> None:
        """Listener for `AfModeChoice`: the page shows the Macro focus toggle state."""
        self.saved = self.saved.model_copy(update={"af_mode": mode})
        self.bus.update_phone(af_mode_choice=mode)

    def in_sensor_zoom_changed(self, enabled: bool) -> None:
        """Listener for `InSensorZoomChoice`: the page shows the toggle state."""
        self.saved = self.saved.model_copy(update={"in_sensor_zoom": enabled})
        self.bus.update_phone(in_sensor_zoom_choice=enabled)

    def orientation_changed(self, orientation: SnapshotOrientation) -> None:
        """Listener for `OrientationState`: the page updates its buttons and reloads the snapshot."""
        self.saved = self.saved.model_copy(update={"snapshot_orientation": orientation})
        self.bus.update_phone(orientation=orientation)

    async def render_snapshot(self, full: bool) -> bytes | None:
        """The last snapshot for the page, in the current orientation: full size, or scaled for the panel.

        Without the raw still (no recorder), the scaled tool result as it was taken.
        """
        raw = self.last_snapshot_raw
        if raw is None:
            return self.last_snapshot
        orientation = self.orientation.current if self.orientation is not None else SnapshotOrientation()
        key = (self.bus.phone.snapshot_seq, orientation, full)
        if key not in self._rendered:
            oriented = await asyncio.to_thread(orient_jpeg, raw, orientation)
            if not full:
                oriented = (await asyncio.to_thread(downscale_jpeg, oriented, images.DEFAULT_MAX_SIDE)).data
            self._rendered[key] = oriented
        return self._rendered[key]

    def phone_snapshot_recorder(self, snapshot: SnapshotReader) -> SnapshotReader:
        """Wrap the phone client snapshot: keep the full JPEG of the running tool call for the full screen view."""

        async def recorded() -> bytes:
            jpeg = await snapshot()
            call = current_call()
            if call is not None:
                self._full_snapshots[call.id] = jpeg
            return jpeg

        return recorded

    def frame_source(self, inner: FrameSource) -> FrameSource:
        return RecordingFrameSource(OnDemandFrameSource(inner, self))

    async def call_nested(self, name: str, arguments: dict[str, Any]) -> tuple[ToolCall, Any]:
        """Run a tool inside another tool (bench_start). It gets its own log row with the same source."""
        outer = current_call()
        return await self._record_call(name, arguments, outer.source if outer is not None else CallSource.MCP)

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
        elif call.tool == tools.BOARD_OPEN and structured is not None and self.board_panel is not None:
            with contextlib.suppress(ValidationError):
                self.board_panel.summary = BoardSummary.model_validate(structured)
        elif call.tool == tools.PHONE_SNAPSHOT and result_images:
            self.last_snapshot = result_images[0]
            # The full image of the same call, for the full screen view. Without it, the scaled one.
            self.last_snapshot_raw = self._full_snapshots.pop(call.id, None)
            self._rendered.clear()
            self.bus.update_phone(has_snapshot=True, snapshot_seq=self.bus.phone.snapshot_seq + 1)

    async def overlay_changed(self, boxes: list[OverlayBox], arrows: list[OverlayArrow]) -> None:
        """The boxes and arrows on the phone changed: the page draws the same ones on its snapshot."""
        self.bus.update_phone(highlights=boxes, arrows=arrows)

    async def tracking_changed(self, state: str) -> None:
        self.bus.update_phone(tracking=str(state))

    def remote_board(self) -> RemoteBoard | None:
        """The last board that another MCP server opened (its board_open call came to this page)."""
        for call in reversed(self.bus.calls()):
            path = call.arguments.get(BOARD_PATH_ARGUMENT)
            if call.tool == tools.BOARD_OPEN and call.origin and call.status is CallStatus.OK and isinstance(path, str):
                return RemoteBoard(path=path, origin=call.origin)
        return None

    async def scene_changed(self, changed_at: datetime) -> None:
        """The board or the phone moved: the page shows a note, unless the live tracking follows the move."""
        if self.bus.phone.tracking != TRACKING_FOLLOWING:
            self.bus.update_phone(scene_changed_at=changed_at)

    def _start_scene_watch(self) -> None:
        if self.scene_watcher is not None:
            self.scene_watcher.start()

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
            self._start_scene_watch()
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
        if self.orientation is not None:
            # OrientationState owns the flips: a page that saves other settings must not change them.
            saved = saved.model_copy(update={"snapshot_orientation": self.orientation.current})
        if self.in_sensor_zoom is not None:
            saved = saved.model_copy(update={"in_sensor_zoom": self.in_sensor_zoom.enabled})
        if self.af_mode is not None:
            saved = saved.model_copy(update={"af_mode": self.af_mode.mode})
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

    async def log_crop_change(self, before: Crop | None) -> None:
        """One log entry for a saved crop change, with a small image of the area that multimeter_read sends."""
        after = self.effective.webcam_crop
        if after == before:
            return
        text = crop_text(after)
        async with self.bus.record(tools.WEBCAM_CROP, {"crop": text}, CallSource.UI) as call:
            call.summary = text
            frame = self.stream.latest if self.stream is not None else None
            if frame is None:
                call.summary = f"{text} (no webcam frame for a preview)"
                return
            call.attach_image(await asyncio.to_thread(crop_preview, frame.jpeg, after), labels.CROP_PREVIEW)

    @property
    def settings_path(self) -> Path:
        return self._store.path

    @property
    def start_settings(self) -> EffectiveSettings:
        return self._start_settings

    # endregion: settings

    # region: lifecycle

    async def start(self) -> None:
        """The server start hook. Eager: the webcam stream and the page now. Lazy: nothing, until a tool needs it."""
        if self.options.start is UiStart.LAZY:
            return
        await self.ensure_webcam()
        await self.ensure_page(auto_open=True)

    # region: page

    async def ensure_page(self, auto_open: bool) -> str:
        """Serve the page, if it does not run yet, and return its URL.

        With `auto_open`, the first start opens the browser when the options say so, but not when another monitor
        runs (that monitor has the window already).
        """
        async with self._page_lock:
            if self._page_stop_task is not None:
                await self._page_stop_task
            if self._server is not None and self.url is not None:
                return self.url
            primary = await self._find_primary()
            if primary is not None:
                # Another server has the page: send the calls there, and serve no page here.
                self.primary_url = primary.url
                if self.forwarder is not None:
                    self.forwarder.start()
                return primary.url
            self.primary_url = None
            await self._serve()
            assert self.url is not None
            url = self.url
        if auto_open and self.options.open_browser and not await self._other_monitor_runs():
            await self.open_browser()
        return url

    async def _ensure_page_quietly(self, auto_open: bool) -> None:
        """A page failure must not fail the tool call."""
        try:
            await self.ensure_page(auto_open)
        except (OSError, RuntimeError, TimeoutError) as exc:
            logger.warning("the monitor page did not start: %s", exc)

    async def _find_primary(self) -> MonitorIdentity | None:
        """The server with the page on the configured port. A secondary waits a short time for it (a dev monitor
        reload takes about 2 s), so it does not take the port while the primary restarts."""
        if self.shared is None:
            return None
        waited = self.primary_url is not None
        deadline = self._clock() + (defaults.PRIMARY_RESTART_GRACE.total_seconds() if waited else 0)
        while True:
            primary = await self.shared.remote.find_monitor()
            if primary is not None or self._clock() >= deadline:
                return primary
            await asyncio.sleep(defaults.PRIMARY_POLL.total_seconds())

    async def _other_monitor_runs(self) -> bool:
        return self.shared is not None and await self.shared.remote.find_monitor() is not None

    async def open_browser(self) -> str | None:
        """Open the page in a new browser window. Return the program, or None when none started."""
        url = self.url or self.primary_url
        if url is None:
            return None
        return await self._opener.open(url)

    async def _serve(self) -> None:
        self.closing = asyncio.Event()
        sock = bind_socket(self.options.host, self.options.port)
        host, port = sock.getsockname()[:2]
        self.url = f"{http.SCHEME}://{host}:{port}/"
        # The secret of the ingest routes. Secondaries read it from this file (mode 600, this user only).
        self.ingest_token, self._token_port = write_token(self.token_dir, port), port
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
                    self._server, self.url = None, None
                    raise RuntimeError("the monitor web server stopped at start")
                await asyncio.sleep(STARTUP_POLL_SECONDS)
        logger.warning("monitor window: %s", self.url)

    async def stop_page(self) -> None:
        """Stop the web server. The event log stays in memory; the next page start shows it again."""
        self.closing.set()
        server, task = self._server, self._serve_task
        self._server, self._serve_task, self.url = None, None, None
        if self.ingest_token is not None and self._token_port is not None:
            remove_token(self.token_dir, self._token_port, self.ingest_token)
            self.ingest_token = None
        if server is not None:
            server.should_exit = True
        if task is not None:
            with contextlib.suppress(Exception):
                await task

    def stop_page_soon(self) -> None:
        """Stop the page after a short delay, so the request that asked for the stop gets its answer."""

        async def later() -> None:
            await asyncio.sleep(defaults.PAGE_STOP_DELAY.total_seconds())
            await self.stop_page()

        self._page_stop_task = asyncio.create_task(later(), name="monitor-page-stop")

    # endregion: page

    # region: webcam

    async def ensure_webcam(self) -> None:
        """Start the webcam stream, or use the stream of another monitor that owns the webcam."""
        self._last_webcam_use = self._clock()
        async with self._webcam_lock:
            if self.stream is None or self.stream.active or self.webcam_owner is not None:
                return
            if self.shared is not None and await self.shared.use_remote_if_present():
                assert self.shared.remote_identity is not None
                self.webcam_owner = self.shared.remote_identity.url
                return
            self.start_stream()

    @contextlib.asynccontextmanager
    async def webcam_user(self) -> AsyncIterator[None]:
        """A tool or another process takes a frame: start the webcam, and keep it on while the frame comes."""
        await self.ensure_webcam()
        self._webcam_users += 1
        try:
            yield
        finally:
            self._webcam_users -= 1
            self._last_webcam_use = self._clock()

    @contextlib.asynccontextmanager
    async def webcam_viewer(self) -> AsyncIterator[None]:
        """A page shows the live view: start the webcam, and keep it on while the page watches."""
        await self.ensure_webcam()
        self._webcam_viewers += 1
        try:
            yield
        finally:
            self._webcam_viewers -= 1
            self._last_webcam_use = self._clock()

    async def start_webcam_now(self) -> str:
        """Start the webcam and wait for the first frame (bench_start). Return a short status."""
        async with self.webcam_user():
            if self.webcam_owner is not None:
                return f"frames from the monitor at {self.webcam_owner}"
            assert self.stream is not None
            latest = self.stream.latest
            await self.stream.next_frame(latest.seq if latest is not None else 0, self.stream.timeout)
            info = self.stream.info()
            return f"{info.device} {info.width}x{info.height}"

    def webcam_idle(self) -> bool:
        timeout = self.options.webcam_idle_timeout
        return (
            self.options.start is UiStart.LAZY
            and timeout > timedelta(0)
            and self.stream is not None
            and self.stream.active
            and self._webcam_users == 0
            and self._webcam_viewers == 0
            and self._clock() - self._last_webcam_use >= timeout.total_seconds()
        )

    async def release_idle_webcam(self) -> bool:
        """Stop the stream when nobody used it for the idle timeout, so other programs can use the camera."""
        if not self.webcam_idle():
            return False
        assert self.stream is not None
        await self.stream.stop()
        logger.warning("webcam %s: stopped after %s without use", self.stream.device, self.options.webcam_idle_timeout)
        return True

    def _start_idle_watch(self) -> None:
        if self.options.start is not UiStart.LAZY or self.options.webcam_idle_timeout <= timedelta(0):
            return
        if self._idle_task is None or self._idle_task.done():
            self._idle_task = asyncio.create_task(self._idle_watch(), name="webcam-idle")

    async def _idle_watch(self) -> None:
        check = min(self.options.webcam_idle_timeout, defaults.WEBCAM_IDLE_CHECK).total_seconds()
        while self.stream is not None and self.stream.active:
            await asyncio.sleep(check)
            if await self.release_idle_webcam():
                return

    async def stop_webcam(self) -> None:
        self.webcam_owner = None
        if self.shared is not None:
            self.shared.remote_identity = None
        if self.stream is not None:
            await self.stream.stop()

    # endregion: webcam

    # region: phone

    async def stop_phone(self) -> None:
        """Stop the phone screen, scrcpy, and the status poll, and remove the adb forward of the camera API."""
        if self.scene_watcher is not None:
            await self.scene_watcher.stop()
        if self.screen is not None:
            await self.screen.stop()
        if self.scrcpy is not None:
            await self.scrcpy.stop()
        if self._status_task is not None:
            self._status_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._status_task
            self._status_task = None
        serial = self.bus.phone.serial
        self.bus.update_phone(scrcpy_running=False)
        if serial is not None and self._forward_remover is not None:
            await self._forward_remover(serial)

    # endregion: phone

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
            self._last_webcam_use = self._clock()
            self._start_idle_watch()

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
        if self._page_stop_task is not None:
            with contextlib.suppress(Exception):
                await self._page_stop_task
        await self.stop_page()
        if self._idle_task is not None:
            self._idle_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._idle_task
        if self.stream is not None:
            await self.stream.stop()
        if self.scrcpy is not None:
            await self.scrcpy.stop()
        if self.scene_watcher is not None:
            await self.scene_watcher.stop()
        if self.screen_mjpeg is not None:
            await self.screen_mjpeg.stop()
        if self.screen is not None:
            await self.screen.stop()
        if self._status_task is not None:
            self._status_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._status_task
        if self.forwarder is not None:
            await self.forwarder.stop()
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
