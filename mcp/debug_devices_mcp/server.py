"""MCP tools for the phone camera, the PC webcam, and the multimeter."""

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, ContentBlock, TextContent
from pydantic import BaseModel, Field

from debug_devices_mcp.adb import Adb, AdbError
from debug_devices_mcp.config import Settings
from debug_devices_mcp.constants import JPEG_FORMAT, SERVER_NAME, images
from debug_devices_mcp.images import downscale_jpeg
from debug_devices_mcp.multimeter import MeterSource, MultimeterReading, VisionClient, VisionError
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
from debug_devices_mcp.webcam import Webcam, WebcamError, WebcamOptions
from debug_devices_mcp.webcam_stream import FrameSource

INSTRUCTIONS = (
    "Tools to see and measure real hardware. Call phone_connect once before the other phone_* tools. "
    "phone_snapshot and webcam_snapshot return a JPEG. multimeter_read returns the value, unit, and mode "
    "that a vision model reads from the webcam that points at the multimeter."
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


# endregion: results


@dataclass
class Services:
    settings: Settings
    adb: Adb
    phone: PhoneClient
    webcam: FrameSource
    vision: VisionClient

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


async def image_result(jpeg: bytes, source: str, max_side: int, save_path: str | None) -> list[TextContent | Image]:
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
    )
    return [TextContent(type="text", text=info.model_dump_json()), Image(data=scaled.data, format=JPEG_FORMAT)]


async def capture_meter_frame(services: Services, source: MeterSource) -> bytes:
    """The JPEG that goes to the vision model.

    Webcam: one frame, with the webcam crop. Phone: one full still, scaled to the default `max_side`.
    """
    if source is MeterSource.WEBCAM:
        return await services.webcam.capture_jpeg()
    jpeg = await services.phone.snapshot()
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
    async def phone_status() -> CameraStatus:
        """Return the zoom ratio, the zoom range, and the torch state of the phone camera."""
        with tool_errors():
            return await services.phone.status()

    @server.tool()
    async def phone_zoom(ratio: float | None = None, step: ZoomStep | None = None) -> CameraStatus:
        """Set the zoom. Give `ratio` (clamped to the camera range) or `step` ("in" x1.5, "out" /1.5), not both."""
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

        The returned image has its long edge scaled to `max_side` pixels (0 = full size). `save_path` writes the
        full-resolution JPEG to disk.
        """
        with tool_errors():
            jpeg = await services.phone.snapshot()
        return await image_result(jpeg, PHONE_SOURCE, max_side, save_path)


# endregion: phone tools

# region: webcam tools


def register_webcam_tools(server: MCPServer, services: Services) -> None:
    @server.tool()
    async def webcam_snapshot(
        save_path: str | None = None, max_side: MaxSide = images.DEFAULT_MAX_SIDE
    ) -> list[TextContent | Image]:
        """Grab one frame from the PC webcam (cropped when --webcam-crop is set) and return it as a JPEG.

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

        `source` "webcam" (default) uses the PC webcam with its crop. "phone" uses a phone snapshot (call
        phone_connect first, and point the phone at the meter). A vision model reads the display, so check
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

    server = MCPServer(SERVER_NAME, instructions=INSTRUCTIONS, lifespan=lifespan)
    if monitor is not None:
        monitor.instrument(server)

    register_phone_tools(server, services)
    register_webcam_tools(server, services)
    return server
