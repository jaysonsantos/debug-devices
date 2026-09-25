"""MCP tools for the phone camera, the PC webcam, and the multimeter."""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Annotated

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, ContentBlock, TextContent
from pydantic import BaseModel, Field, ValidationError

from debug_devices_mcp.adb import Adb, AdbError
from debug_devices_mcp.board.constants import defaults as board_defaults
from debug_devices_mcp.board.loader import BoardviewKeys, LoaderOptions
from debug_devices_mcp.board.tools import BoardSession, register_board_tools
from debug_devices_mcp.camera_choice import (
    AF_MODE_NOT_ON_PHONE,
    AfModeChoice,
    AfModeSync,
    InSensorZoomChoice,
    InSensorZoomSync,
)
from debug_devices_mcp.config import Settings
from debug_devices_mcp.constants import JPEG_FORMAT, SERVER_NAME, images, phone
from debug_devices_mcp.focus import (
    FocusSource,
    PhoneStatusReport,
    PointOutsideError,
    SnapshotGeometry,
    snapshot_focus_request,
)
from debug_devices_mcp.highlight import (
    BoxOutsideError,
    HighlightResult,
    PixelBox,
    draw_boxes,
    overlay_box,
    scale_boxes,
)
from debug_devices_mcp.images import SnapshotOrientation, downscale_jpeg, orient_jpeg
from debug_devices_mcp.instructions import register_instructions_tool, server_instructions
from debug_devices_mcp.multimeter import MeterSource, MultimeterReading, VisionClient, VisionError
from debug_devices_mcp.orientation import OrientationState, PreviewSync
from debug_devices_mcp.phone_api import (
    AfModeName,
    ApiErrorCode,
    CameraStatus,
    Health,
    OverlayArrow,
    OverlayBox,
    OverlayRequest,
    PhoneApiError,
    PhoneClient,
    PhoneError,
    PhoneUnreachableError,
    RotationAutoRequest,
    RotationDegrees,
    RotationLockRequest,
    ScreenFocusRequest,
    SnapshotFocusRequest,
    ZoomRatioRequest,
    ZoomStep,
    ZoomStepRequest,
)
from debug_devices_mcp.pointer import PointResult
from debug_devices_mcp.pointing import Pointing
from debug_devices_mcp.process import SubprocessRunner
from debug_devices_mcp.remote_webcam import RemoteMonitor, SharedWebcam
from debug_devices_mcp.scene import SceneState
from debug_devices_mcp.ui.monitor import Monitor
from debug_devices_mcp.ui.settings import SettingsStore, state_dir
from debug_devices_mcp.ui.tools import register_monitor_tools
from debug_devices_mcp.webcam import Webcam, WebcamError, WebcamOptions
from debug_devices_mcp.webcam_stream import FrameSource

TOOL_GUIDE = (
    "Tools to see and measure real hardware. Call phone_connect once before the other phone_* tools. "
    "phone_snapshot and webcam_snapshot return a JPEG. multimeter_read returns the value, unit, and mode "
    "that a vision model reads from the webcam that points at the multimeter. "
    "The local monitor page shows the webcam, the phone screen, and every tool call: monitor_open starts it and "
    'returns its URL; tell the user the URL. bench_start starts everything in one call ("start the bench"), '
    'bench_stop stops it and frees the camera ("stop the bench").'
)


PHONE_SOURCE = "phone"
NO_SNAPSHOT_YET = "take a phone_snapshot first: {what} are pixels in the last phone_snapshot image"

logger = logging.getLogger(__name__)

type OverlayListener = Callable[[list[OverlayBox], list[OverlayArrow]], Awaitable[None]]

type MaxSide = Annotated[int, Field(ge=0, description="Long edge in pixels of the returned image. 0 = full size.")]

# region: results


class PhoneConnection(BaseModel):
    serial: str
    local_port: int
    started_app: bool
    health: Health
    status: CameraStatus


class SnapshotInfo(BaseModel):
    source: str
    width: int
    height: int
    size_bytes: int
    original_width: int
    original_height: int
    original_size_bytes: int
    saved_to: str | None
    # Phone only: the flips that the user chose (phone_snapshot_orientation). Positions refer to this photo.
    orientation: str | None = None


# endregion: results


@dataclass
class Services:
    settings: Settings
    adb: Adb
    phone: PhoneClient
    webcam: FrameSource
    vision: VisionClient
    board: BoardSession = field(
        default_factory=lambda: BoardSession.create(
            SubprocessRunner(), LoaderOptions(dump_bin=board_defaults.DUMP_BIN, timeout=board_defaults.DUMP_TIMEOUT)
        )
    )
    # The flips of every phone snapshot: the tools, the saved files, and the monitor page use them.
    orientation: OrientationState = field(default_factory=OrientationState)
    # Sends the same flips to the phone preview, so the phone screen matches the snapshots.
    preview_sync: PreviewSync = field(init=False)
    # The user's in-sensor zoom choice, and the sync that sends it again after an app start.
    in_sensor_zoom: InSensorZoomChoice = field(default_factory=InSensorZoomChoice)
    in_sensor_zoom_sync: InSensorZoomSync = field(init=False)
    # The user's autofocus mode choice (continuous or macro), and its sync.
    af_mode: AfModeChoice = field(default_factory=AfModeChoice)
    af_mode_sync: AfModeSync = field(init=False)
    # The last phone_snapshot image that the agent got: phone_focus and phone_highlight map its pixels back.
    last_snapshot: SnapshotGeometry | None = None
    last_snapshot_image: bytes | None = None
    # Did the board or the phone move since the last phone_snapshot? The monitor's watcher marks it.
    scene: SceneState = field(default_factory=SceneState)
    # The highlight boxes and arrows on the phone now (true orientation, 0 to 1; angles in degrees).
    highlights: list[OverlayBox] = field(default_factory=list)
    arrows: list[OverlayArrow] = field(default_factory=list)
    # Searched parts and the live tracker that keeps their boxes and arrows on the board.
    pointing: Pointing = field(init=False)
    _overlay_listeners: list[OverlayListener] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.preview_sync = PreviewSync(self.phone, self.orientation)
        self.in_sensor_zoom_sync = InSensorZoomSync(self.phone, self.in_sensor_zoom)
        self.af_mode_sync = AfModeSync(self.phone, self.af_mode)
        self.pointing = Pointing(self)
        self.scene.add_listener(self._scene_changed)
        self.scene.add_frame_listener(self.pointing.on_frame)

    def connect_board(self) -> None:
        """The board photo tools refuse work after a scene change, point to parts, and start the live tracking."""
        self.board.scene_guard = self.guard_registration
        self.board.highlighter = self.highlight_parts
        self.board.on_registered = self.pointing.registered

    def add_overlay_listener(self, listener: OverlayListener) -> None:
        self._overlay_listeners.append(listener)

    def guard_registration(self, registration_id: str | None) -> None:
        """Photo positions are refused after a move, unless the live tracker follows that registration."""
        if registration_id is not None and self.pointing.tracked(registration_id):
            return
        self.scene.guard()

    async def send_overlay(self, boxes: list[OverlayBox], arrows: list[OverlayArrow]) -> CameraStatus:
        """Show these boxes and arrows on the phone (they replace the old ones), and tell the page."""
        with self.camera_command(), tool_errors():
            status = await self.phone.overlay(OverlayRequest(boxes=boxes, arrows=arrows))
        await self._overlay_changed(boxes, arrows)
        return status

    async def _overlay_changed(self, boxes: list[OverlayBox], arrows: list[OverlayArrow]) -> None:
        self.highlights, self.arrows = boxes, arrows
        for listener in self._overlay_listeners:
            await listener(boxes, arrows)

    async def highlight_parts(
        self, registration_id: str, refdes: list[str], boxes: list[PixelBox], photo_size: tuple[int, int]
    ) -> tuple[PointResult, bytes | None]:
        """board_locate_in_photo with `highlight`: point to the parts, and draw them on the last snapshot."""
        result = await self.pointing.point_to(refdes, registration_id)
        geometry, image = self.last_snapshot, self.last_snapshot_image
        if geometry is None or image is None or not boxes:
            return result, None
        try:
            scaled = scale_boxes(boxes, photo_size, (geometry.width, geometry.height))
            return result, await asyncio.to_thread(draw_boxes, image, scaled)
        except BoxOutsideError:
            return result, None

    @contextmanager
    def camera_command(self) -> Iterator[None]:
        """Our own command changes the phone picture: the scene watcher takes a new reference after it."""
        self.scene.own_command()
        try:
            yield
        finally:
            self.scene.own_command()

    async def _scene_changed(self, _: datetime) -> None:
        """The board or the phone moved: the boxes and the photo registrations are for the old scene, unless the
        live tracker follows the move (then its registration and the pointed parts stay)."""
        tracker = self.pointing.tracker
        if await self.pointing.scene_changed() and tracker is not None:
            for registration in self.board.registrations.values():
                registration.stale = registration.registration_id != tracker.registration_id
            if self.pointing.target is not None or not self.highlights:
                return
        else:
            self.board.mark_registrations_stale()
        self.pointing.stop()
        if not self.highlights and not self.arrows:
            return
        try:
            await self.phone.overlay(OverlayRequest(boxes=[]))
        except PhoneError as exc:
            logger.warning("cannot clear the phone highlight boxes after a scene change: %s", exc)
        await self._overlay_changed([], [])

    async def highlight(
        self, boxes: list[PixelBox], photo_size: tuple[int, int] | None = None
    ) -> tuple[HighlightResult, bytes]:
        """Send boxes in pixels of the last phone_snapshot (or of a photo of `photo_size`: the same photo at
        another size) to the phone. Returns the result and the annotated last snapshot."""
        self.scene.guard()
        geometry, image = self.last_snapshot, self.last_snapshot_image
        if geometry is None or image is None:
            raise ToolError(NO_SNAPSHOT_YET.format(what="the boxes"))
        try:
            if photo_size is not None:
                boxes = scale_boxes(boxes, photo_size, (geometry.width, geometry.height))
            overlay = [overlay_box(box, geometry) for box in boxes]
            request = OverlayRequest(boxes=overlay)
        except (BoxOutsideError, ValidationError) as exc:
            raise ToolError(f"the boxes are not valid: {exc}") from exc
        self.pointing.stop()
        status = await self.send_overlay(request.boxes, [])
        annotated = await asyncio.to_thread(draw_boxes, image, boxes)
        result = HighlightResult(
            count=len(overlay), boxes=overlay, overlay_boxes=status.overlay_boxes, note=HighlightResult.ESTIMATE_NOTE
        )
        return result, annotated

    async def clear_highlights(self) -> HighlightResult:
        self.pointing.stop()
        status = await self.send_overlay([], [])
        return HighlightResult(count=0, boxes=[], overlay_boxes=status.overlay_boxes, note=HighlightResult.CLEARED_NOTE)

    def reset_phone_syncs(self) -> None:
        """A new phone_connect: try the newer endpoints again (the app can have an update)."""
        self.preview_sync.reset()
        self.in_sensor_zoom_sync.reset()
        self.af_mode_sync.reset()

    async def sync_phone(self, status: CameraStatus) -> CameraStatus:
        """Bring the app back to the user's choices when a status shows other ones (for example after an app start)."""
        status = await self.preview_sync.ensure(status)
        status = await self.in_sensor_zoom_sync.ensure(status)
        return await self.af_mode_sync.ensure(status)

    async def phone_snapshot(self) -> bytes:
        """One phone still in the orientation that the user chose (the true bytes when there is no flip)."""
        jpeg = await self.phone.snapshot()
        orientation = self.orientation.current
        try:
            return await asyncio.to_thread(orient_jpeg, jpeg, orientation)
        except (OSError, ValueError) as exc:
            raise ToolError(f"{PHONE_SOURCE} returned an image that cannot be decoded: {exc}") from exc

    @classmethod
    def from_settings(cls, settings: Settings) -> Services:
        runner = SubprocessRunner()
        vision = VisionClient(
            settings.openrouter_api_key, settings.vision_model, settings.openrouter_base_url, settings.vision_timeout
        )
        vision.meter_model = settings.meter_model
        return cls(
            settings=settings,
            adb=Adb(runner, settings.adb_path, settings.adb_timeout),
            phone=PhoneClient.for_local_port(
                settings.local_forward_port, settings.phone_http_timeout, settings.phone_snapshot_timeout
            ),
            # When another MCP process owns the busy webcam, take the frames from its monitor.
            webcam=SharedWebcam(
                Webcam(
                    runner,
                    WebcamOptions(
                        ffmpeg_path=settings.ffmpeg_path,
                        device=settings.webcam,
                        warmup_frames=settings.webcam_warmup_frames,
                        timeout=settings.webcam_timeout,
                        crop=settings.webcam_crop,
                    ),
                ),
                RemoteMonitor(settings.ui_port, settings.webcam_timeout),
            ),
            vision=vision,
            orientation=OrientationState(SettingsStore.in_dir(state_dir())),
            in_sensor_zoom=InSensorZoomChoice(SettingsStore.in_dir(state_dir())),
            af_mode=AfModeChoice(SettingsStore.in_dir(state_dir())),
            board=BoardSession.create(
                runner,
                LoaderOptions(
                    dump_bin=settings.obv_dump_path,
                    timeout=settings.boardview_dump_timeout,
                    keys=BoardviewKeys(
                        fz=settings.boardview_fz_key, cae=settings.boardview_cae_key, xzz=settings.boardview_xzz_key
                    ),
                ),
            ),
        )

    async def aclose(self) -> None:
        await self.phone.aclose()
        await self.vision.aclose()


@contextmanager
def tool_errors() -> Iterator[None]:
    """Turn expected failures into `ToolError`, so the model reads the message."""
    try:
        yield
    except PhoneUnreachableError as exc:
        raise ToolError(f"{exc}. Call phone_connect first, and check that the app runs on the phone.") from exc
    except (PhoneError, AdbError, WebcamError, VisionError) as exc:
        raise ToolError(str(exc)) from exc


def save_jpeg(jpeg: bytes, save_path: str | None) -> Path | None:
    if not save_path:
        return None
    path = Path(save_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(jpeg)
    return path


async def image_result(
    jpeg: bytes, source: str, max_side: int, save_path: str | None, orientation: str | None = None
) -> list[TextContent | Image]:
    """Save the full image when asked, and return a copy with the long edge at most `max_side`."""
    saved_to = await asyncio.to_thread(save_jpeg, jpeg, save_path)
    try:
        scaled = await asyncio.to_thread(downscale_jpeg, jpeg, max_side)
    except (OSError, ValueError) as exc:
        raise ToolError(f"{source} returned an image that cannot be decoded: {exc}") from exc
    info = SnapshotInfo(
        source=source,
        width=scaled.width,
        height=scaled.height,
        size_bytes=len(scaled.data),
        original_width=scaled.original_width,
        original_height=scaled.original_height,
        original_size_bytes=len(jpeg),
        saved_to=str(saved_to) if saved_to else None,
        orientation=orientation,
    )
    return [TextContent(type="text", text=info.model_dump_json()), Image(data=scaled.data, format=JPEG_FORMAT)]


async def capture_meter_frame(services: Services, source: MeterSource) -> bytes:
    """The JPEG that goes to the vision model.

    Webcam: one frame, with the webcam crop. Phone: one full still, scaled to the default `max_side`.
    """
    if source is MeterSource.WEBCAM:
        return await services.webcam.capture_jpeg()
    jpeg = await services.phone_snapshot()
    try:
        scaled = await asyncio.to_thread(downscale_jpeg, jpeg, images.DEFAULT_MAX_SIDE)
    except (OSError, ValueError) as exc:
        raise ToolError(f"{PHONE_SOURCE} returned an image that cannot be decoded: {exc}") from exc
    return scaled.data


async def wait_until_ready(services: Services) -> tuple[Health, CameraStatus]:
    """Poll health, then status, until the camera is bound or the app start timeout ends."""
    settings = services.settings
    async with asyncio.timeout(settings.app_start_timeout.total_seconds()):
        while True:
            try:
                health = await services.phone.health()
                return health, await services.phone.status()
            except PhoneUnreachableError:
                pass
            except PhoneApiError as exc:
                if exc.error.error != ApiErrorCode.CAMERA_NOT_READY:
                    raise
            await asyncio.sleep(settings.poll_interval.total_seconds())


async def connect_phone(services: Services) -> PhoneConnection:
    settings = services.settings
    device = await services.adb.select_device(settings.adb_serial)
    await services.adb.forward(device.serial, settings.local_forward_port)
    started_app = False
    try:
        await services.phone.health()
    except PhoneError:
        await services.adb.start_app(device.serial)
        started_app = True
    try:
        health, status = await wait_until_ready(services)
    except TimeoutError as exc:
        raise ToolError(
            f"camera app on {device.serial} is not ready after {settings.app_start_timeout}. "
            "Unlock the phone and check that the app is installed and has the camera permission."
        ) from exc
    # After an app start the preview is not flipped: send the chosen flips again.
    services.reset_phone_syncs()
    status = await services.sync_phone(status)
    return PhoneConnection(
        serial=device.serial,
        local_port=settings.local_forward_port,
        started_app=started_app,
        health=health,
        status=status,
    )


# region: phone tools


def register_phone_tools(server: MCPServer, services: Services) -> None:
    @server.tool()
    async def phone_connect() -> PhoneConnection:
        """Find the phone over ADB, forward the camera API port, and start the camera app when it does not answer.

        With several ADB devices, set --adb-serial (DEBUG_DEVICES_ADB_SERIAL). Returns the app health and the
        camera status.
        """
        with tool_errors():
            return await connect_phone(services)

    @server.tool()
    async def phone_status() -> PhoneStatusReport:
        """Return the zoom, the torch, and the rotation of the phone camera, and how far the phone is from the board.

        `distance_cm` (lens focus distance), `detail_px_per_mm` (how many snapshot pixels one mm of the board gets),
        `advice` (`too_close`, `good`, `far`, `unknown`), and `advice_text`. Tell the user to move the phone when
        the advice is `far` or `too_close`. The values are estimates; `calibration` says how good the distance is.
        `scene_changed` (with `scene_changed_at`) is true when the board or the phone moved since the last
        phone_snapshot: take a fresh phone_snapshot before you point at anything.
        """
        with tool_errors():
            # A status with other preview flips means that the app restarted: send the flips again.
            status = await services.sync_phone(await services.phone.status())
        return PhoneStatusReport.of(status).with_scene(services.scene.changed_at)

    @server.tool()
    async def phone_zoom(ratio: float | None = None, step: ZoomStep | None = None) -> CameraStatus:
        """Set the zoom. Give `ratio` (clamped to the camera range) or `step` ("in" x1.5, "out" /1.5), not both.

        To locate a part: zoom in to read small markings and check nearby components, zoom out for wider context.
        Take a fresh phone_snapshot after each zoom change and inspect the relevant area of that photo.
        """
        if (ratio is None) == (step is None):
            raise ToolError("give exactly one of `ratio` or `step`")
        request = ZoomRatioRequest(ratio=ratio) if ratio is not None else ZoomStepRequest(step=step)
        with services.camera_command(), tool_errors():
            return await services.phone.zoom(request)

    @server.tool()
    async def phone_torch(enabled: bool) -> CameraStatus:
        """Turn the phone torch (flash LED) on or off."""
        with services.camera_command(), tool_errors():
            return await services.phone.torch(enabled)

    @server.tool()
    async def phone_rotation(degrees: RotationDegrees | None = None, auto: bool = False) -> CameraStatus:
        """Set the rotation of the next phone snapshots.

        Give `degrees` (0, 90, 180, or 270) to lock the rotation, or `auto: true` to follow the physical orientation
        of the phone again. Not both. The status shows `rotation_degrees` and `rotation_locked`.
        """
        if (degrees is None) == (not auto):
            raise ToolError("give exactly one of `degrees` or `auto: true`")
        request = RotationLockRequest(degrees=degrees) if degrees is not None else RotationAutoRequest()
        with services.camera_command(), tool_errors():
            return await services.phone.rotation(request)

    @server.tool()
    async def phone_snapshot(
        save_path: str | None = None, max_side: MaxSide = images.DEFAULT_MAX_SIDE
    ) -> list[TextContent | Image]:
        """Take one full still with the phone back camera and return it as a JPEG.

        Use it for every question about what is visible on the device (parts, markings, damage, orientation). Take a
        fresh one for each such question and after each phone_zoom change. For a visible marking, quote it exactly,
        then call board_match_marking. Boardview data tells you where to look, but it does not prove what the photo
        shows. A part on the other board side cannot be confirmed from this photo: ask the user to isolate the power
        safely before they turn the board.
        The returned image has its long edge scaled to `max_side` pixels (0 = full size). `save_path` writes the
        full-resolution JPEG to disk.
        The user can flip the photo horizontally and vertically (phone_snapshot_orientation, or the monitor page).
        The photo comes in that orientation, the same as the user sees it; the text part says which. Pixel positions
        (board_register_photo, board_match_marking x_px/y_px) refer to this oriented photo.
        """
        orientation = services.orientation.current
        with tool_errors():
            jpeg = await services.phone_snapshot()
        content = await image_result(jpeg, PHONE_SOURCE, max_side, save_path, orientation.describe())
        info = SnapshotInfo.model_validate_json(content[0].text)
        services.last_snapshot = SnapshotGeometry(width=info.width, height=info.height, orientation=orientation)
        services.last_snapshot_image = content[1].data  # type: ignore[union-attr]
        # This photo is the scene now: a later move of the board or the phone makes it stale.
        services.scene.snapshot_taken()
        return content

    @server.tool()
    async def phone_snapshot_orientation(
        flip_horizontal: bool | None = None, flip_vertical: bool | None = None
    ) -> SnapshotOrientation:
        """Read or set the flips of the phone snapshots. No argument: only read. A value sets that flip.

        Use it when the user says the photo is mirrored (left-right: flip_horizontal) or upside down
        (flip_vertical). The setting persists and applies to phone_snapshot, multimeter_read with the phone, and
        the monitor page. The phone mirrors its camera preview the same way (its status text stays readable);
        the phone camera and its zoom do not change.
        """
        orientation = services.orientation.update(flip_horizontal, flip_vertical)
        # The phone preview follows (only the camera image; the app's text stays readable).
        with services.camera_command():
            await services.preview_sync.push()
        return orientation


def register_camera_tools(server: MCPServer, services: Services) -> None:
    """The camera settings on the phone: the focus point and the in-sensor zoom."""

    @server.tool()
    async def phone_focus(x: float, y: float, source: FocusSource = FocusSource.SNAPSHOT) -> PhoneStatusReport:
        """Focus (and meter) the phone camera on one point, for example a blurry part.

        `source` "snapshot" (default): `x`, `y` are pixels in the last phone_snapshot image that you got, as you
        saw it (after the flips and the scaling). `source` "screen": `x`, `y` are a point on the phone screen from
        0 to 1. The focus holds about 5 s. Take a fresh phone_snapshot after the focus settles (about 1 s) to see
        the effect.
        """
        try:
            if source is FocusSource.SCREEN:
                request: ScreenFocusRequest | SnapshotFocusRequest = ScreenFocusRequest(screen_x=x, screen_y=y)
            elif services.last_snapshot is None:
                raise ToolError(NO_SNAPSHOT_YET.format(what="x and y"))
            else:
                services.scene.guard()
                request = snapshot_focus_request(x, y, services.last_snapshot)
        except (ValidationError, PointOutsideError) as exc:
            raise ToolError(f"the focus point is not valid: {exc}") from exc
        with services.camera_command(), tool_errors():
            status = await services.phone.focus(request)
        return PhoneStatusReport.of(status)

    @server.tool()
    async def phone_in_sensor_zoom(enabled: bool) -> PhoneStatusReport:
        """Turn the in-sensor zoom on or off. Real extra detail at 2x-4x from a sensor crop (not optics) on phones
        that support it; the preview stops about 1 s while the camera rebinds.

        `in_sensor_zoom` in the result: `on`, `off`, `unsupported` (this phone has no such mode), or `fallback` (the
        mode failed; the camera runs normally). The choice persists and comes back after an app restart. Take a
        fresh phone_snapshot after the change; this is not optical zoom.
        """
        services.in_sensor_zoom.set(enabled)
        with services.camera_command(), tool_errors():
            status = await services.in_sensor_zoom_sync.send()
        return PhoneStatusReport.of(status)

    @server.tool()
    async def phone_af_mode(mode: AfModeName) -> PhoneStatusReport:
        """Set the autofocus mode. macro: the camera's close-range autofocus mode, for work near the minimum focus
        distance (about 10-12 cm). continuous: the normal autofocus.

        `af_mode` in the result is the mode now. A phone without the macro mode stays `continuous`. The choice
        persists and comes back after an app restart. Take a fresh phone_snapshot after the change.
        """
        services.af_mode.set(mode)
        with services.camera_command(), tool_errors():
            status = await services.af_mode_sync.send()
        report = PhoneStatusReport.of(status)
        if mode == "macro" and status.af_mode is not None and status.af_mode.value != mode:
            report.advice_text = f"{AF_MODE_NOT_ON_PHONE}. {report.advice_text}".strip()
        return report

    @server.tool()
    async def phone_highlight(
        boxes: list[PixelBox] | None = None, clear: bool = False
    ) -> Annotated[CallToolResult, HighlightResult]:
        """Draw a green box around a part that you found in the last phone_snapshot. Quote the marking you see
        first. The box is your estimate; say so. Clear it when done or when the phone moves.

        `boxes`: up to 8, each `{x, y, width, height, label}` in pixels of the last phone_snapshot image that you
        got (as you saw it: after the flips and the scaling); `label` at most 32 characters, for example the
        marking. New boxes replace the old ones. `clear: true` removes all boxes. The phone draws the boxes over
        its camera preview (the monitor page live view shows them), and the page draws them on its snapshot. The
        result has a copy of your last snapshot with the boxes: check that each box is where you meant. After the
        board or the phone moves, the server clears the boxes and refuses new ones until a fresh phone_snapshot.
        """
        if clear:
            if boxes:
                raise ToolError("give `boxes` or `clear: true`, not both")
            result = await services.clear_highlights()
            content: list[ContentBlock] = [TextContent(type="text", text=result.model_dump_json())]
        elif not boxes:
            raise ToolError("give `boxes`, or `clear: true` to remove the boxes")
        else:
            result, annotated = await services.highlight(boxes)
            content = [
                TextContent(type="text", text=result.model_dump_json()),
                Image(data=annotated, format=JPEG_FORMAT).to_image_content(),
            ]
        return CallToolResult(content=content, structured_content=result.model_dump(mode="json"))

    @server.tool()
    async def phone_point_to(refdes: str | list[str], registration_id: str | None = None) -> PointResult:
        """Point the user to parts on the board: a green box when a part is in the phone view, else an arrow at
        the view edge toward it, with the board distance (for example "J4 ~4 cm").

        Needs board_open and a photo registration (board_register_photo) of the board side that the phone sees;
        without `registration_id`, the newest one. With live tracking (the result of board_register_photo says
        it), the boxes and arrows follow the board while the user moves the phone. Tell the user to follow the
        arrow and the distance; do not say left or right, because that depends on how they hold the phone. A part
        on the other board side gets no arrow: tell the user to isolate the power before they turn the board.
        The positions are boardview estimates: say so. Remove them with phone_highlight `clear: true`.
        """
        names = [refdes] if isinstance(refdes, str) else refdes
        if not names or len(names) > phone.OVERLAY_MAX_BOXES:
            raise ToolError(f"give 1 to {phone.OVERLAY_MAX_BOXES} parts in `refdes`")
        return await services.pointing.point_to(names, registration_id)


# endregion: phone tools

# region: webcam tools


def register_webcam_tools(server: MCPServer, services: Services) -> None:
    @server.tool()
    async def webcam_snapshot(
        save_path: str | None = None, max_side: MaxSide = images.DEFAULT_MAX_SIDE
    ) -> list[TextContent | Image]:
        """Grab one frame from the PC webcam (cropped when --webcam-crop is set) and return it as a JPEG.

        Use it only to check the multimeter framing and the crop. Not for questions about the device (use
        phone_snapshot), and not to read the meter (use multimeter_read).
        The returned image has its long edge scaled to `max_side` pixels (0 = full size). `save_path` writes the
        full-resolution JPEG to disk.
        """
        with tool_errors():
            jpeg = await services.webcam.capture_jpeg()
        return await image_result(jpeg, str(services.webcam.device), max_side, save_path)

    @server.tool()
    async def multimeter_read(
        include_image: bool = False, source: MeterSource = MeterSource.WEBCAM
    ) -> Annotated[CallToolResult, MultimeterReading]:
        """Read the multimeter: value, unit, display text, mode, range, and flags.

        This is the only tool for meter values. Never read a meter value yourself from an image.
        `source` "webcam" (default) uses the PC webcam with its crop. "phone" uses a phone snapshot (call
        phone_connect first). The image goes to the vision model, so use "phone" only when the phone points at the
        meter, never when it points at the board. A vision model reads the display, so check
        `readable` and `confidence`. `include_image` also returns the image that the model saw.
        """
        with tool_errors():
            services.vision.require_api_key()
            jpeg = await capture_meter_frame(services, source)
            reading = await services.vision.read_multimeter(jpeg)
        content: list[ContentBlock] = [TextContent(type="text", text=reading.model_dump_json())]
        if include_image:
            content.append(Image(data=jpeg, format=JPEG_FORMAT).to_image_content())
        return CallToolResult(content=content, structured_content=reading.model_dump(mode="json"))


# endregion: webcam tools


def build_server(
    services: Services, monitor: Monitor | None = None, after_stop: Callable[[], object] | None = None
) -> MCPServer:
    """`monitor` records every tool call and runs the monitor window while the server runs."""

    @asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[None]:
        try:
            if monitor is not None:
                await monitor.start()
            yield
        finally:
            if monitor is not None:
                await monitor.stop()
            await services.aclose()
            if after_stop is not None:
                after_stop()

    instructions_file = services.settings.instructions_file
    instructions = server_instructions(instructions_file, TOOL_GUIDE)
    server = MCPServer(SERVER_NAME, instructions=instructions, lifespan=lifespan)
    if monitor is not None:
        monitor.instrument(server)

    register_instructions_tool(server, instructions_file)
    register_phone_tools(server, services)
    register_camera_tools(server, services)
    register_webcam_tools(server, services)
    services.connect_board()
    register_board_tools(server, services.board)
    if monitor is not None:
        register_monitor_tools(server, monitor)
    return server
