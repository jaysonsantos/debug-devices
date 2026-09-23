"""MCP tools for the phone camera, the PC webcam, and the multimeter."""

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, ContentBlock, TextContent
from pydantic import BaseModel

from debug_devices_mcp.adb import Adb, AdbError
from debug_devices_mcp.config import Settings
from debug_devices_mcp.constants import JPEG_FORMAT, SERVER_NAME
from debug_devices_mcp.multimeter import MultimeterReading, VisionClient, VisionError
from debug_devices_mcp.phone_api import (
    ApiErrorCode,
    CameraStatus,
    Health,
    PhoneApiError,
    PhoneClient,
    PhoneError,
    PhoneUnreachableError,
    ZoomRatioRequest,
    ZoomStep,
    ZoomStepRequest,
)
from debug_devices_mcp.process import SubprocessRunner
from debug_devices_mcp.webcam import Webcam, WebcamError

INSTRUCTIONS = (
    "Tools to see and measure real hardware. Call phone_connect once before the other phone_* tools. "
    "phone_snapshot and webcam_snapshot return a JPEG. multimeter_read returns the value, unit, and mode "
    "that a vision model reads from the webcam that points at the multimeter."
)


# region: results


class PhoneConnection(BaseModel):
    serial: str
    local_port: int
    started_app: bool
    health: Health
    status: CameraStatus


class SnapshotInfo(BaseModel):
    source: str
    size_bytes: int
    saved_to: str | None


# endregion: results


@dataclass
class Services:
    settings: Settings
    adb: Adb
    phone: PhoneClient
    webcam: Webcam
    vision: VisionClient

    @classmethod
    def from_settings(cls, settings: Settings) -> Services:
        runner = SubprocessRunner()
        return cls(
            settings=settings,
            adb=Adb(runner, settings.adb_path, settings.adb_timeout),
            phone=PhoneClient.for_local_port(
                settings.local_forward_port, settings.phone_http_timeout, settings.phone_snapshot_timeout
            ),
            webcam=Webcam(
                runner, settings.ffmpeg_path, settings.webcam, settings.webcam_warmup_frames, settings.webcam_timeout
            ),
            vision=VisionClient(
                settings.openrouter_api_key,
                settings.vision_model,
                settings.openrouter_base_url,
                settings.vision_timeout,
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


def image_result(jpeg: bytes, source: str, saved_to: Path | None) -> list[TextContent | Image]:
    info = SnapshotInfo(source=source, size_bytes=len(jpeg), saved_to=str(saved_to) if saved_to else None)
    return [TextContent(type="text", text=info.model_dump_json()), Image(data=jpeg, format=JPEG_FORMAT)]


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


def build_server(services: Services) -> MCPServer:
    @asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await services.aclose()

    server = MCPServer(SERVER_NAME, instructions=INSTRUCTIONS, lifespan=lifespan)

    # region: phone

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
    async def phone_snapshot(save_path: str | None = None) -> list[TextContent | Image]:
        """Take one full still with the phone back camera. Returns the JPEG. `save_path` also writes it to disk."""
        with tool_errors():
            jpeg = await services.phone.snapshot()
        return image_result(jpeg, "phone", save_jpeg(jpeg, save_path))

    # endregion: phone

    # region: webcam

    @server.tool()
    async def webcam_snapshot(save_path: str | None = None) -> list[TextContent | Image]:
        """Grab one frame from the PC webcam. Returns the JPEG. `save_path` also writes it to disk."""
        with tool_errors():
            jpeg = await services.webcam.capture_jpeg()
        return image_result(jpeg, str(services.webcam.device), save_jpeg(jpeg, save_path))

    @server.tool()
    async def multimeter_read(include_image: bool = False) -> Annotated[CallToolResult, MultimeterReading]:
        """Read the multimeter that the webcam sees: value, unit, display text, mode, range, and flags.

        A vision model reads the display, so check `readable` and `confidence`. `include_image` also returns
        the webcam frame.
        """
        with tool_errors():
            jpeg = await services.webcam.capture_jpeg()
            reading = await services.vision.read_multimeter(jpeg)
        content: list[ContentBlock] = [TextContent(type="text", text=reading.model_dump_json())]
        if include_image:
            content.append(Image(data=jpeg, format=JPEG_FORMAT).to_image_content())
        return CallToolResult(content=content, structured_content=reading.model_dump(mode="json"))

    # endregion: webcam

    return server
