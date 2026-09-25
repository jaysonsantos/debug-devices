"""MCP tools for the phone camera, the PC webcam, and the multimeter."""

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, ContentBlock, TextContent
from pydantic import BaseModel, Field

from debug_devices_mcp.adb import Adb, AdbError
from debug_devices_mcp.board.constants import defaults as board_defaults
from debug_devices_mcp.board.loader import BoardviewKeys, LoaderOptions
from debug_devices_mcp.board.tools import BoardSession, register_board_tools
from debug_devices_mcp.config import Settings
from debug_devices_mcp.constants import JPEG_FORMAT, SERVER_NAME, images
from debug_devices_mcp.focus import PhoneStatusReport
from debug_devices_mcp.images import SnapshotOrientation, downscale_jpeg, orient_jpeg
from debug_devices_mcp.instructions import register_instructions_tool, server_instructions
from debug_devices_mcp.multimeter import MeterSource, MultimeterReading, VisionClient, VisionError
from debug_devices_mcp.orientation import OrientationState, PreviewSync
from debug_devices_mcp.phone_api import (
    ApiErrorCode,
    CameraStatus,
    Health,
    PhoneApiError,
    PhoneClient,
    PhoneError,
    PhoneUnreachableError,
    RotationAutoRequest,
    RotationDegrees,
    RotationLockRequest,
    ZoomRatioRequest,
    ZoomStep,
    ZoomStepRequest,
)
from debug_devices_mcp.process import SubprocessRunner
from debug_devices_mcp.remote_webcam import RemoteMonitor, SharedWebcam
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

    def __post_init__(self) -> None:
        self.preview_sync = PreviewSync(self.phone, self.orientation)

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
    services.preview_sync.reset()
    status = await services.preview_sync.ensure(status)
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
        """
        with tool_errors():
            # A status with other preview flips means that the app restarted: send the flips again.
            status = await services.preview_sync.ensure(await services.phone.status())
        return PhoneStatusReport.of(status)

    @server.tool()
    async def phone_zoom(ratio: float | None = None, step: ZoomStep | None = None) -> CameraStatus:
        """Set the zoom. Give `ratio` (clamped to the camera range) or `step` ("in" x1.5, "out" /1.5), not both.

        To locate a part: zoom in to read small markings and check nearby components, zoom out for wider context.
        Take a fresh phone_snapshot after each zoom change and inspect the relevant area of that photo.
        """
        if (ratio is None) == (step is None):
            raise ToolError("give exactly one of `ratio` or `step`")
        request = ZoomRatioRequest(ratio=ratio) if ratio is not None else ZoomStepRequest(step=step)
        with tool_errors():
            return await services.phone.zoom(request)

    @server.tool()
    async def phone_torch(enabled: bool) -> CameraStatus:
        """Turn the phone torch (flash LED) on or off."""
        with tool_errors():
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
        with tool_errors():
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
        orientation = services.orientation.current.describe()
        with tool_errors():
            jpeg = await services.phone_snapshot()
        return await image_result(jpeg, PHONE_SOURCE, max_side, save_path, orientation)

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
        await services.preview_sync.push()
        return orientation


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
    register_webcam_tools(server, services)
    register_board_tools(server, services.board)
    if monitor is not None:
        register_monitor_tools(server, monitor)
    return server
