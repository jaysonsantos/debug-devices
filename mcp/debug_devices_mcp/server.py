"""MCP tools for the phone camera, the PC webcam, and the multimeter."""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from http import HTTPStatus
from pathlib import Path
from typing import Annotated, Any

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, ContentBlock, TextContent
from pydantic import AwareDatetime, BaseModel, Field, ValidationError

from debug_devices_mcp.adb import Adb, AdbDevice, AdbError
from debug_devices_mcp.app_restart import (
    PHONE_TOOL_PREFIX,
    RESTART_STALE_REASON,
    AppRestartWatch,
    RestartNotice,
    restart_notice,
)
from debug_devices_mcp.bench_state import (
    BenchStateStore,
    add_photo,
    current_step_text,
    recent_user_mode,
    register_bench_state_tools,
)
from debug_devices_mcp.board.constants import defaults as board_defaults
from debug_devices_mcp.board.loader import BoardviewKeys, LoaderOptions
from debug_devices_mcp.board.session_store import SESSION_FILE_NAME, BoardSessionStore
from debug_devices_mcp.board.tools import BoardSession, register_board_tools
from debug_devices_mcp.camera_choice import (
    AF_MODE_NOT_ON_PHONE,
    AfModeChoice,
    AfModeSync,
    InSensorZoomChoice,
    InSensorZoomSync,
    MarkingsChoice,
    MarkingsSync,
)
from debug_devices_mcp.config import Settings
from debug_devices_mcp.constants import JPEG_FORMAT, SERVER_NAME, images, phone
from debug_devices_mcp.constants import defaults as core_defaults
from debug_devices_mcp.devices import DeviceList, PhoneSelection, Transport, list_devices, transport_of
from debug_devices_mcp.evidence import (
    CameraView,
    Capture,
    CaptureKind,
    CaptureLog,
    register_evidence_tools,
    tag_image,
)
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
    draw_highlights,
    overlay_box,
    scale_boxes,
    visibility,
    with_tags,
)
from debug_devices_mcp.images import SnapshotOrientation, downscale_jpeg, transform_jpeg
from debug_devices_mcp.instructions import register_instructions_tool, server_instructions
from debug_devices_mcp.meter_frames import DEFAULT_FRAMES, MAX_FRAMES, MIN_FRAMES, MeterLimits, combine
from debug_devices_mcp.multimeter import (
    MeterMode,
    MeterResult,
    MeterSource,
    MultimeterReading,
    VisionClient,
    VisionError,
    check_reading,
)
from debug_devices_mcp.orientation import ImageTransform, OrientationState, PreviewSync
from debug_devices_mcp.phone_api import (
    OLD_APP_MARKINGS,
    AfModeName,
    ApiErrorCode,
    CameraStatus,
    Health,
    OverlayArrow,
    OverlayBox,
    OverlayNotSupportedError,
    OverlayRequest,
    PhoneApiError,
    PhoneClient,
    PhoneError,
    PhoneUnreachableError,
    PreviewRegion,
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
from debug_devices_mcp.pointing import CarryResult, Pointing
from debug_devices_mcp.process import SubprocessRunner
from debug_devices_mcp.remote_webcam import RemoteMonitor, SharedWebcam
from debug_devices_mcp.scene import SceneState
from debug_devices_mcp.schematic import SchematicFinder, SchematicOptions, register_schematic_tools
from debug_devices_mcp.snapshot_crop import CropInfo, CropOutsideError, SnapshotCrop, crop_snapshot
from debug_devices_mcp.ui.constants import tools as tool_names
from debug_devices_mcp.ui.monitor import Monitor
from debug_devices_mcp.ui.settings import SettingsStore, state_dir
from debug_devices_mcp.ui.tools import register_monitor_tools
from debug_devices_mcp.webcam import Webcam, WebcamError, WebcamOptions
from debug_devices_mcp.webcam_controls import (
    CONTROLS_FILE_NAME,
    DEFAULT_V4L2_CTL,
    V4l2Controls,
    WebcamControlStore,
    register_webcam_control_tools,
)
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
# For the next phone tool result after a move without live tracking.
SCENE_CLEARED_NOTE = "the board or the phone moved: the server cleared the boxes and arrows"

logger = logging.getLogger(__name__)

type OverlayListener = Callable[[list[OverlayBox], list[OverlayArrow]], Awaitable[None]]
type RestartListener = Callable[[RestartNotice | None], Awaitable[None]]

type MaxSide = Annotated[int, Field(ge=0, description="Long edge in pixels of the returned image. 0 = full size.")]

# region: results


class MarkingsResult(BaseModel):
    visible: bool
    # What the phone did: "shown", "hidden", or why not (an old app keeps its own boxes visible).
    phone: str


MARKINGS_HIDDEN = (
    "markings are hidden in the monitor: the user does not see the boxes and arrows now (they appear when the user "
    "shows the markings again)"
)
# An old app cannot hide its own boxes: the server removed them from the phone and keeps them for later.
OLD_APP_HIDDEN = f"{OLD_APP_MARKINGS}, so the server removed them from the phone (they come back when shown)"


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
    # Phone only: the final transform of the still (the same as the monitor preview): a clockwise turn, then the
    # flips. Positions refer to this photo.
    orientation: str | None = None
    turn_degrees: int | None = None
    flip_horizontal: bool | None = None
    flip_vertical: bool | None = None
    # Phone only, with `crop`: the area of the extra full-resolution image.
    crop: CropInfo | None = None
    # Phone only: the newest photo registration after the board or the phone moved: carried over by image
    # features to this snapshot (a new registration_id), or still stale with the reason.
    registration: CarryResult | None = None
    # Phone snapshots: the capture id (UUID v7) and UTC time. A position claim names this id (capture_status).
    capture_id: str | None = None
    captured_at: AwareDatetime | None = None


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
    # The phone that the user selected in the monitor page (over --adb-serial); only the user selects it.
    selection: PhoneSelection = field(default_factory=PhoneSelection)
    # Show or hide the markings (the page's drawings and the phone's boxes and arrows), and its sync.
    markings: MarkingsChoice = field(default_factory=MarkingsChoice)
    markings_sync: MarkingsSync = field(init=False)
    # The user's autofocus mode choice (continuous or macro), and its sync.
    af_mode: AfModeChoice = field(default_factory=AfModeChoice)
    af_mode_sync: AfModeSync = field(init=False)
    # The last phone_snapshot image that the agent got: phone_focus and phone_highlight map its pixels back.
    last_snapshot: SnapshotGeometry | None = None
    last_snapshot_image: bytes | None = None
    # Did the board or the phone move since the last phone_snapshot? The monitor's watcher marks it.
    scene: SceneState = field(default_factory=SceneState)
    # Capture ids of phone snapshots and meter images; a scene change starts a new epoch (evidence.py).
    captures: CaptureLog = field(default_factory=CaptureLog)
    # The local bench record (bench_state.py). Memory only unless from_settings gives it the file.
    bench: BenchStateStore = field(default_factory=BenchStateStore)
    # The camera view (zoom, lens, transform) of the last phone still: its capture records it (evidence.py).
    last_view: CameraView | None = None
    # The meter webcam image controls (v4l2-ctl); memory only unless from_settings gives the saved file.
    webcam_controls: V4l2Controls = field(
        default_factory=lambda: V4l2Controls(
            SubprocessRunner(), core_defaults.WEBCAM, WebcamControlStore(), DEFAULT_V4L2_CTL
        )
    )
    # The highlight boxes and arrows on the phone now (true orientation, 0 to 1; angles in degrees).
    highlights: list[OverlayBox] = field(default_factory=list)
    arrows: list[OverlayArrow] = field(default_factory=list)
    # The part of the still that the phone screen shows (CameraStatus.preview_region of the last status).
    preview_region: PreviewRegion | None = None
    # The app can hide its own boxes (CameraStatus.overlay_visible of the last status). False: an old app; while the
    # markings are hidden, the server keeps the boxes and arrows off the phone.
    app_hides_markings: bool = True
    # Searched parts and the live tracker that keeps their boxes and arrows on the board.
    pointing: Pointing = field(init=False)
    # The phone app restarted (a new app_start_id): the notice goes with every phone tool result until the agent's
    # next phone_snapshot (app_restart.py).
    app_restart: AppRestartWatch = field(default_factory=AppRestartWatch)
    restart_notice: RestartNotice | None = None
    # One short sentence for the agent's next phone tool result: the server changed the boxes and arrows without a
    # tool call (for example the live tracking lost the board and cleared them).
    phone_note: str | None = None
    _overlay_listeners: list[OverlayListener] = field(default_factory=list)
    _restart_listeners: list[RestartListener] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.preview_sync = PreviewSync(self.phone, self.orientation)
        self.in_sensor_zoom_sync = InSensorZoomSync(self.phone, self.in_sensor_zoom)
        self.af_mode_sync = AfModeSync(self.phone, self.af_mode)
        self.markings_sync = MarkingsSync(self.phone, self.markings)
        self.pointing = Pointing(self)
        self.scene.add_listener(self._scene_changed)
        self.scene.add_frame_listener(self.pointing.on_frame)
        self.captures.attach(self.scene)

    def connect_board(self) -> None:
        """The board photo tools refuse work after a scene change, point to parts, and start the live tracking."""
        self.board.scene_guard = self.guard_registration
        self.board.highlighter = self.highlight_parts
        self.board.on_registered = self.pointing.registered
        # Part identity claims check their photo ids against the capture log (evidence.py).
        self.board.photos = self.captures
        # A registration remembers its photo; pixel tools check that the last photo still fits it.
        self.board.current_photo_id = self._latest_photo_id
        self.board.snapshot_guard = self.guard_snapshot_registration

    def _latest_photo_id(self) -> str | None:
        latest = self.captures.latest_photo
        return latest.capture_id if latest is not None else None

    def add_overlay_listener(self, listener: OverlayListener) -> None:
        self._overlay_listeners.append(listener)

    def add_restart_listener(self, listener: RestartListener) -> None:
        self._restart_listeners.append(listener)

    async def check_app_start(self, status: CameraStatus) -> None:
        """A new app_start_id: the app is back at its defaults. Clear what belongs to the old camera view."""
        if not self.app_restart.restarted(status):
            return
        turn = self.orientation.transform(status.rotation_degrees).turn_degrees
        notice = restart_notice(status, turn)
        logger.info("%s", notice.message)
        self.board.mark_registrations_stale(RESTART_STALE_REASON)
        await self.pointing.stop_tracking()
        # The restarted app has no boxes: only our record and the page need the change.
        if self.highlights or self.arrows:
            await self._overlay_changed([], [])
        await self.set_restart_notice(notice)

    def overlay_note(self, note: str) -> None:
        self.phone_note = note

    async def set_restart_notice(self, notice: RestartNotice | None) -> None:
        self.restart_notice = notice
        for listener in self._restart_listeners:
            await listener(notice)

    def guard_registration(self, registration_id: str | None) -> None:
        """Photo positions are refused after a move, unless the live tracker follows that registration."""
        if registration_id is not None and self.pointing.tracked(registration_id):
            return
        self.scene.guard()
        self.guard_snapshot_registration(registration_id)

    def guard_snapshot_registration(self, registration_id: str | None) -> None:
        """Pixels of the last phone_snapshot can use a registration of an older photo only in the same scene and with
        the same camera view (zoom, lens, transform). The live tracker does not help here: it maps live frames."""
        registration = self.board.registrations.get(registration_id or "")
        # A stale registration gets its own, clearer message from BoardSession.registration().
        if registration is None or registration.stale:
            return
        problem = self.captures.reuse_problem(registration.photo_id)
        if problem is not None:
            raise ToolError(f"registration {registration.registration_id}: {problem}")

    async def send_overlay(self, boxes: list[OverlayBox], arrows: list[OverlayArrow]) -> CameraStatus:
        """Show these boxes and arrows on the phone (they replace the old ones), and tell the page. While the markings
        are hidden, an old app gets none (it cannot hide them): set_markings sends them when shown again."""
        on_phone = ([], []) if self.server_hides_markings() else (boxes, arrows)
        with self.camera_command(), tool_errors():
            status = await self._post_overlay(*on_phone)
        self.seen_status(status)
        await self._overlay_changed(boxes, arrows)
        return status

    async def _post_overlay(self, boxes: list[OverlayBox], arrows: list[OverlayArrow]) -> CameraStatus:
        try:
            return await self.phone.overlay(OverlayRequest(boxes=boxes, arrows=arrows))
        except PhoneApiError as exc:
            if exc.status != HTTPStatus.BAD_REQUEST or not any(box.tag for box in boxes):
                raise
            logger.info("the phone app does not take box tags (an older app): sent without them")
            untagged = [box.model_copy(update={"tag": None}) for box in boxes]
            return await self.phone.overlay(OverlayRequest(boxes=untagged, arrows=arrows))

    def server_hides_markings(self) -> bool:
        """Markings hidden and an old app: the server keeps the boxes and arrows off the phone."""
        return not self.app_hides_markings and not self.markings.visible

    async def set_markings(self, visible: bool) -> MarkingsResult:
        """The page's Markings toggle: save the choice, then hide or show the phone's own boxes and arrows."""
        self.markings.set(visible)
        try:
            with self.camera_command():
                status = await self.markings_sync.send()
        except OverlayNotSupportedError:
            return await self._set_old_app_markings(visible)
        except PhoneError as exc:
            return MarkingsResult(visible=visible, phone=f"not sent to the phone now: {exc}")
        if status.overlay_visible is None:
            return await self._set_old_app_markings(visible)
        self.seen_status(status)
        return MarkingsResult(visible=visible, phone="shown" if status.overlay_visible else "hidden")

    async def _set_old_app_markings(self, visible: bool) -> MarkingsResult:
        """An old app cannot hide its boxes: remove them from the phone, or send them again. Our record and the page
        keep them."""
        self.app_hides_markings = False
        boxes, arrows = (self.highlights, self.arrows) if visible else ([], [])
        try:
            with self.camera_command():
                status = await self._post_overlay(boxes, arrows)
        except OverlayNotSupportedError as exc:
            return MarkingsResult(visible=visible, phone=str(exc))
        except PhoneError as exc:
            return MarkingsResult(visible=visible, phone=f"not sent to the phone now: {exc}")
        self.seen_status(status)
        return MarkingsResult(visible=visible, phone="shown" if visible else OLD_APP_HIDDEN)

    def markings_note(self) -> str | None:
        """For the tool results: new boxes stay hidden until the user shows the markings again."""
        return None if self.markings.visible else MARKINGS_HIDDEN

    def seen_status(self, status: CameraStatus) -> None:
        """Keep what later calls need from a status: the preview region, and whether the app can hide its boxes."""
        if status.preview_region is not None:
            self.preview_region = status.preview_region
        self.app_hides_markings = status.overlay_visible is not None

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
        """The board or the phone moved: the boxes and the photo registrations are for the old scene, unless a
        live tracker follows the move (then its registration stays, and the boxes and arrows that it moves stay)."""
        tracker = self.pointing.tracker
        if await self.pointing.scene_changed() and tracker is not None:
            for registration in self.board.registrations.values():
                registration.stale = registration.registration_id != tracker.registration_id
        else:
            self.board.mark_registrations_stale()
        if self.pointing.overlay_tracked():
            return
        await self.pointing.stop()
        if not self.highlights and not self.arrows:
            return
        try:
            await self.send_overlay([], [])
        except ToolError as exc:
            logger.warning("cannot clear the phone highlight boxes after a scene change: %s", exc)
            await self._overlay_changed([], [])
        self.overlay_note(SCENE_CLEARED_NOTE)

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
            # One tag per box: the phone, the annotated image, and the page show the same tags.
            boxes = with_tags(boxes)
            overlay = [overlay_box(box, geometry) for box in boxes]
            request = OverlayRequest(boxes=overlay)
        except (BoxOutsideError, ValidationError) as exc:
            raise ToolError(f"the boxes are not valid: {exc}") from exc
        await self.pointing.stop()
        status = await self.send_overlay(request.boxes, [])
        # The boxes follow the board while the phone moves, when the phone screen stream matches the snapshot.
        tracking = await self.pointing.follow_plain_boxes(boxes, request.boxes)
        annotated, layout_summary = await asyncio.to_thread(draw_highlights, image, boxes)
        seen, warning = visibility(overlay, status.preview_region or self.preview_region)
        result = HighlightResult(
            count=len(overlay),
            boxes=overlay,
            overlay_boxes=status.overlay_boxes,
            note=HighlightResult.ESTIMATE_NOTE,
            visibility=seen,
            warning=warning,
            markings=self.markings_note(),
            layout=layout_summary,
            tracking=tracking,
        )
        return result, annotated

    async def clear_highlights(self) -> HighlightResult:
        await self.pointing.stop()
        status = await self.send_overlay([], [])
        return HighlightResult(count=0, boxes=[], overlay_boxes=status.overlay_boxes, note=HighlightResult.CLEARED_NOTE)

    def reset_phone_syncs(self) -> None:
        """A new phone_connect: try the newer endpoints again (the app can have an update)."""
        self.preview_sync.reset()
        self.in_sensor_zoom_sync.reset()
        self.af_mode_sync.reset()
        self.markings_sync.reset()

    async def sync_phone(self, status: CameraStatus) -> CameraStatus:
        """Bring the app back to the stored choices after an app restart (app_start.py has the rule). The page sees a
        change that another MCP server wrote to the settings file."""
        self.orientation.refresh()
        self.in_sensor_zoom.refresh()
        self.af_mode.refresh()
        self.markings.refresh()
        self.seen_status(status)
        await self.check_app_start(status)
        status = await self.preview_sync.ensure(status)
        status = await self.in_sensor_zoom_sync.ensure(status)
        status = await self.af_mode_sync.ensure(status)
        return await self.markings_sync.ensure(status)

    async def phone_snapshot(self) -> tuple[bytes, ImageTransform]:
        """One phone still, shown like the monitor preview: turned by the remaining turn, then the user's flips.

        The status gives the rotation of the still that the app takes now (orientation.py has the geometry).
        """
        status = await self.phone.status()
        self.seen_status(status)
        await self.check_app_start(status)
        transform = self.orientation.transform(status.rotation_degrees)
        self.last_view = CameraView.of(status, transform)
        jpeg = await self.phone.snapshot()
        try:
            shown = await asyncio.to_thread(
                transform_jpeg, jpeg, transform.turn_degrees, transform.flip_horizontal, transform.flip_vertical
            )
        except (OSError, ValueError) as exc:
            raise ToolError(f"{PHONE_SOURCE} returned an image that cannot be decoded: {exc}") from exc
        return shown, transform

    @classmethod
    def from_settings(cls, settings: Settings) -> Services:
        runner = SubprocessRunner()
        vision = VisionClient(
            settings.openrouter_api_key, settings.vision_model, settings.openrouter_base_url, settings.vision_timeout
        )
        vision.meter_model = settings.meter_model
        vision.meter_counts = settings.meter_counts
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
            markings=MarkingsChoice(SettingsStore.in_dir(state_dir())),
            selection=PhoneSelection(SettingsStore.in_dir(state_dir())),
            bench=BenchStateStore(settings.bench_state_file),
            webcam_controls=V4l2Controls(
                runner,
                settings.webcam,
                WebcamControlStore(state_dir() / CONTROLS_FILE_NAME),
                settings.v4l2_ctl_path,
            ),
            board=BoardSession.create(
                runner,
                LoaderOptions(
                    dump_bin=settings.obv_dump_path,
                    timeout=settings.boardview_dump_timeout,
                    keys=BoardviewKeys(
                        fz=settings.boardview_fz_key, cae=settings.boardview_cae_key, xzz=settings.boardview_xzz_key
                    ),
                ),
            ).with_store(BoardSessionStore(state_dir() / SESSION_FILE_NAME)),
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
    jpeg: bytes, source: str, max_side: int, save_path: str | None, transform: ImageTransform | None = None
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
        orientation=transform.describe() if transform is not None else None,
        turn_degrees=transform.turn_degrees if transform is not None else None,
        flip_horizontal=transform.flip_horizontal if transform is not None else None,
        flip_vertical=transform.flip_vertical if transform is not None else None,
    )
    return [TextContent(type="text", text=info.model_dump_json()), Image(data=scaled.data, format=JPEG_FORMAT)]


async def capture_meter_frame(services: Services, source: MeterSource) -> bytes:
    """The JPEG that goes to the vision model.

    Webcam: one frame, with the webcam crop. Phone: one full still, scaled to the default `max_side`.
    """
    if source is MeterSource.WEBCAM:
        return await services.webcam.capture_jpeg()
    jpeg, _ = await services.phone_snapshot()
    try:
        scaled = await asyncio.to_thread(downscale_jpeg, jpeg, images.DEFAULT_MAX_SIDE)
    except (OSError, ValueError) as exc:
        raise ToolError(f"{PHONE_SOURCE} returned an image that cannot be decoded: {exc}") from exc
    return scaled.data


async def take_phone_snapshot(
    services: Services, save_path: str | None, max_side: int, crop: SnapshotCrop | None = None
) -> list[ContentBlock]:
    """The phone_snapshot tool (also used by bench_measure): the oriented still, its capture id, and the image.
    With `crop`: also a full-resolution crop of an area, enlarged (snapshot_crop.py)."""
    with tool_errors():
        jpeg, transform = await services.phone_snapshot()
    content = await image_result(jpeg, PHONE_SOURCE, max_side, save_path, transform)
    info = SnapshotInfo.model_validate_json(content[0].text)
    crop_image = None
    if crop is not None:
        try:
            crop_jpeg, crop_info = await asyncio.to_thread(crop_snapshot, jpeg, (info.width, info.height), crop)
        except CropOutsideError as exc:
            raise ToolError(str(exc)) from exc
        info = info.model_copy(update={"crop": crop_info})
        crop_image = Image(data=crop_jpeg, format=JPEG_FORMAT).to_image_content()
    services.last_snapshot = SnapshotGeometry(width=info.width, height=info.height, orientation=transform)
    services.last_snapshot_image = content[1].data  # type: ignore[union-attr]
    # This photo is the scene now: a later move of the board or the phone makes it stale.
    services.scene.snapshot_taken()
    # region: capture id (evidence.py)
    capture = services.captures.record(CaptureKind.PHONE_SNAPSHOT, PHONE_SOURCE, services.last_view)
    # The bench record keeps every photo id (bench_state.py).
    await asyncio.to_thread(add_photo, services.bench, capture.capture_id)
    info = info.model_copy(update={"capture_id": capture.capture_id, "captured_at": capture.captured_at})
    # A registration that went stale (the board or the phone moved) comes along when the images match; the
    # carried one gets this capture id as its photo.
    carry = await services.pointing.carry()
    if carry is not None:
        info = info.model_copy(update={"registration": carry})
    image = tag_image(content[1].to_image_content(), capture)  # type: ignore[union-attr]
    blocks: list[ContentBlock] = [TextContent(type="text", text=info.model_dump_json()), image]
    if crop_image is not None:
        blocks.append(tag_image(crop_image, capture))
    return blocks
    # endregion: capture id


async def read_meter(
    services: Services,
    source: MeterSource,
    expected_mode: MeterMode | None,
    frames: int,
    expected_value: float | None = None,
) -> tuple[MeterResult, list[tuple[bytes, Capture]]]:
    """Capture `frames` meter images (the interval apart), read them in parallel, and combine the checked results.

    Every image gets its own capture id; the result has the id of the first one, and every id finds the result.
    """
    settings = services.settings
    limits = MeterLimits(max_volts=settings.max_voltage, max_amps=settings.max_current)
    user_mode = recent_user_mode(services.bench)
    captured: list[tuple[bytes, Capture]] = []
    reads: list[asyncio.Task[MultimeterReading]] = []
    with tool_errors():
        services.vision.require_api_key()
        if source is MeterSource.WEBCAM:
            await services.webcam_controls.ensure_applied()
        try:
            for index in range(frames):
                if index:
                    await asyncio.sleep(settings.meter_frame_interval.total_seconds())
                jpeg = await capture_meter_frame(services, source)
                # The id of this exact image: the result and the returned image name it.
                captured.append((jpeg, services.captures.record(CaptureKind.METER_IMAGE, source.value)))
                reads.append(asyncio.create_task(services.vision.read_multimeter(jpeg)))
            readings = await asyncio.gather(*reads)
        finally:
            for task in reads:
                task.cancel()
    meter_model = services.vision.meter_model
    results = [
        check_reading(reading, expected_mode, meter_model, user_mode, settings.meter_counts).model_copy(
            update={"capture_id": capture.capture_id, "captured_at": capture.captured_at}
        )
        for reading, (_, capture) in zip(readings, captured, strict=True)
    ]
    result = combine(results, limits, user_mode, expected_value=expected_value, counts=settings.meter_counts)
    for _, capture in captured:
        services.captures.meter_results[capture.capture_id] = result
    return result, captured


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


async def select_phone(services: Services) -> AdbDevice:
    """The selected phone (page, then config). A lost Wi-Fi phone gets one `adb connect` before the error."""
    serial, _ = services.selection.effective(services.settings.adb_serial)
    try:
        return await services.adb.select_device(serial)
    except AdbError as first_error:
        if not serial or transport_of(serial) is not Transport.WIFI:
            raise
        try:
            await services.adb.connect(serial)
        except AdbError as exc:
            raise AdbError(f"{first_error}; adb connect {serial} failed too: {exc}") from exc
        return await services.adb.select_device(serial)


async def connect_phone(services: Services) -> PhoneConnection:
    settings = services.settings
    device = await select_phone(services)
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

        The phone is the one that the user selected in the monitor page (Devices), else --adb-serial
        (DEBUG_DEVICES_ADB_SERIAL), else the only device. With several devices and no choice, ask the user to select
        the phone in the monitor page. Returns the app health and the camera status.
        """
        with tool_errors():
            return await connect_phone(services)

    @server.tool()
    async def phone_devices() -> DeviceList:
        """List the phones that adb sees: serial, USB or Wi-Fi, state, model, and if our camera app is installed.

        Read-only: only the user selects the phone in the monitor page (Devices); you cannot select, pair, or
        connect a phone. Tell the user when the phone they need is not selected, not authorized, or has no app.
        """
        return await list_devices(services.adb, services.selection, services.settings.adb_serial)

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
        save_path: str | None = None, max_side: MaxSide = images.DEFAULT_MAX_SIDE, crop: SnapshotCrop | None = None
    ) -> list[ContentBlock]:
        """Take one full still with the phone back camera and return it as a JPEG.

        Use it for every question about what is visible on the device (parts, markings, damage, orientation). Take a
        fresh one for each such question and after each phone_zoom change. For a visible marking, quote it exactly,
        then call board_match_marking. Boardview data tells you where to look, but it does not prove what the photo
        shows. A part on the other board side cannot be confirmed from this photo: ask the user to isolate the power
        safely before they turn the board.
        The returned image has its long edge scaled to `max_side` pixels (0 = full size). `save_path` writes the
        full-resolution JPEG to disk.
        The photo is turned and flipped exactly like the monitor preview of the user (the Screen view choice, the
        phone rotation, and the flips of phone_snapshot_orientation), so you and the user see the same picture. The
        text part says the final turn and flips (`turn_degrees`, `flip_horizontal`, `flip_vertical`). Pixel
        positions (board_register_photo, board_match_marking x_px/y_px, phone_focus, phone_highlight) refer to this
        photo as you got it.
        `crop` (optional): `{x, y, radius}` or `{box: {x, y, width, height}}` in pixels of this photo as you get
        it, and `zoom` (1-8, default 2). A second image is that area from the full-resolution still, enlarged, for
        example to check where a probe tip touches or to read a small marking; the text part says the area
        (`crop`). You judge the crop yourself.
        """
        return await take_phone_snapshot(services, save_path, max_side, crop)

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
        result has a copy of your last snapshot with the boxes: check that each box is where you meant.
        With live tracking (`tracking` in the result), the boxes follow the board while the phone moves, and a box
        outside the view becomes an arrow at the edge. Without it, or when the tracking loses the board, the server
        clears the boxes after a move, and the next phone tool result says so. After a move the boxes stay
        estimates: take a fresh phone_snapshot before you say what is visible. New boxes need a fresh
        phone_snapshot after a move.
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
        result = await services.pointing.point_to(names, registration_id)
        return result.model_copy(update={"markings": services.markings_note()})


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
        await services.webcam_controls.ensure_applied()
        with tool_errors():
            jpeg = await services.webcam.capture_jpeg()
        return await image_result(jpeg, str(services.webcam.device), max_side, save_path)

    @server.tool()
    async def multimeter_read(
        include_image: bool = False,
        source: MeterSource = MeterSource.WEBCAM,
        expected_mode: MeterMode | None = None,
        frames: Annotated[int, Field(ge=MIN_FRAMES, le=MAX_FRAMES)] = DEFAULT_FRAMES,
        expected_value: float | None = None,
    ) -> Annotated[CallToolResult, MeterResult]:
        """Read the multimeter, then check the reading. This is the only tool for meter values.

        Never read a meter value yourself from an image. It reads `frames` frames (default 2, about 1 s apart); each
        frame is checked, then they are combined. The result has the model's reading as evidence (`display_text` is
        the exact LCD text, `unit`, `mode`, and each frame in `frames`) and a checked `status`:
        "confirmed" (unit and mode agree, confidence high enough, the same digits and decimal point in every frame,
        and below --max-voltage/--max-current: `value` is the measurement), "uncertain" (unit symbol not readable, low
        confidence, digits that change: see `value_min`/`value_max`), "disputed" (the unit does not fit the mode, a
        moved decimal point, a value above the bench limit, or not the `expected_mode`), or "unreadable". Only
        "confirmed" has a numeric `value`; for the others, report no numeric conclusion and follow `request`.
        `expected_mode` is the mode of the current test: context only, it never confirms the LCD mode. A mode that the
        user confirmed on the dial in the last 10 minutes (bench_state_update meter_mode_confirmed_by_user) is used
        for the check (`mode_source: "user"`, the model's mode in `model_mode`).
        `expected_value` is the nominal value of the test in the base unit of the mode (V, A, or Ω; for example 3.3
        for a 3.3 V rail): pass it when the test has one. It is context, never proof: a value outside a third to three
        times of it becomes "uncertain" (a misplaced decimal point is likely), and `problems` names a point shift that
        fits. With --meter-counts, a volt or ampere reading that the display cannot show (more digits, fewer digits in
        V or A without a prefix, above the count, an impossible leading zero) is "uncertain". `digits` and
        `digits_before_point` are the model's reading of the digits and the point; they must agree with
        `display_text` and `value`.
        `source` "webcam" (default) uses the PC webcam with its crop. "phone" uses a phone snapshot (call
        phone_connect first). The image goes to the vision model, so use "phone" only when the phone points at the
        meter, never when it points at the board. `include_image` also returns the exact images that the model saw.
        """
        result, captured = await read_meter(services, source, expected_mode, frames, expected_value)
        content: list[ContentBlock] = [TextContent(type="text", text=result.model_dump_json())]
        if include_image:
            content += [
                tag_image(Image(data=jpeg, format=JPEG_FORMAT).to_image_content(), capture)
                for jpeg, capture in captured
            ]
        return CallToolResult(content=content, structured_content=result.model_dump(mode="json"))


# endregion: webcam tools

# region: bench measure (a meter value and the photo of the probe contact, taken together)


class BenchMeasurement(BaseModel):
    measurement_id: str
    meter: MeterResult
    photo: SnapshotInfo
    # Seconds from the first meter frame to the phone photo (both start at the same time).
    gap_seconds: float
    note: str


BENCH_MEASURE_NOTE = (
    "The meter value and the photo belong together: name both capture ids (meter.capture_id, photo.capture_id) "
    'when you report the value and where the probes touch. Only a meter status "confirmed" is a measurement.'
)


def register_bench_measure_tools(server: MCPServer, services: Services) -> None:
    @server.tool()
    async def bench_measure(
        expected_mode: MeterMode | None = None,
        frames: Annotated[int, Field(ge=MIN_FRAMES, le=MAX_FRAMES)] = DEFAULT_FRAMES,
        save_path: str | None = None,
        max_side: MaxSide = images.DEFAULT_MAX_SIDE,
        include_meter_images: bool = False,
        expected_value: float | None = None,
    ) -> Annotated[CallToolResult, BenchMeasurement]:
        """Read the meter (webcam, `frames` frames) and take a fresh phone_snapshot at the same moment.

        Use it for a measurement that needs the photo of the probe contact: the value and the photo belong together
        (both capture ids are in the result). The meter part is the same as multimeter_read (only "confirmed" is a
        measurement; `expected_value` is the nominal value of the test, context only); the photo part is the same as
        phone_snapshot (the phone points at the board).
        """
        meter_task = asyncio.create_task(
            read_meter(services, MeterSource.WEBCAM, expected_mode, frames, expected_value)
        )
        try:
            photo_content = await take_phone_snapshot(services, save_path, max_side)
        except BaseException:
            meter_task.cancel()
            raise
        meter, captured = await meter_task
        photo = SnapshotInfo.model_validate_json(photo_content[0].text)  # type: ignore[union-attr]
        gap = 0.0
        if photo.captured_at is not None and meter.captured_at is not None:
            gap = abs((photo.captured_at - meter.captured_at).total_seconds())
        result = BenchMeasurement(
            measurement_id=str(uuid.uuid7()),
            meter=meter,
            photo=photo,
            gap_seconds=round(gap, 2),
            note=BENCH_MEASURE_NOTE,
        )
        content: list[ContentBlock] = [TextContent(type="text", text=result.model_dump_json()), photo_content[1]]
        if include_meter_images:
            content += [
                tag_image(Image(data=jpeg, format=JPEG_FORMAT).to_image_content(), capture)
                for jpeg, capture in captured
            ]
        return CallToolResult(content=content, structured_content=result.model_dump(mode="json"))


# endregion: bench measure


def add_phone_notes(server: MCPServer, services: Services) -> None:
    """Phone tool results end with the server's notes. After a phone app restart, every phone tool result has the
    notice; the agent's next phone_snapshot (a call from the MCP client, not from the page) shows it one last time.
    A `phone_note` (for example: the live tracking lost the board and cleared the boxes) goes to the agent's next
    phone tool result only."""
    call_tool = server.call_tool

    async def with_notes(name: str, arguments: dict[str, Any], context: Any = None) -> Any:
        result = await call_tool(name, arguments, context)
        if not name.startswith(PHONE_TOOL_PREFIX) or not isinstance(result, CallToolResult):
            return result
        notice, note = services.restart_notice, services.phone_note
        notes = [text for text in (note, notice.message if notice is not None else None) if text is not None]
        if not notes:
            return result
        from_agent = context is not None and not result.is_error
        if from_agent:
            services.phone_note = None
        if notice is not None and from_agent and name == tool_names.PHONE_SNAPSHOT:
            await services.set_restart_notice(None)
        content = [*result.content, *(TextContent(type="text", text=text) for text in notes)]
        return result.model_copy(update={"content": content})

    server.call_tool = with_notes  # type: ignore[method-assign]


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
    add_phone_notes(server, services)
    if monitor is not None:
        monitor.instrument(server)

    settings = services.settings
    schematic = SchematicFinder(
        SubprocessRunner(),
        SchematicOptions(
            path=settings.schematic,
            pdftotext=settings.pdftotext_path,
            pdftoppm=settings.pdftoppm_path,
            timeout=settings.schematic_timeout,
        ),
    )
    register_instructions_tool(server, instructions_file, partial(current_step_text, services.bench), schematic.note)
    register_schematic_tools(server, schematic)
    register_phone_tools(server, services)
    register_camera_tools(server, services)
    register_webcam_tools(server, services)
    register_evidence_tools(server, services.captures)
    register_bench_measure_tools(server, services)
    register_webcam_control_tools(server, services.webcam_controls)
    register_bench_state_tools(server, services.bench, services.captures)
    services.connect_board()
    register_board_tools(server, services.board)
    if monitor is not None:
        register_monitor_tools(server, monitor)
    return server
