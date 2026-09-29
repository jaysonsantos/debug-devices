"""The live webcam view: an MJPEG stream from the shared capture, its size, and a stream of only the crop box (the
meter picture of the full-screen phone view)."""

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from datetime import timedelta
from typing import TYPE_CHECKING

from pydantic import BaseModel, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from debug_devices_mcp.ui.constants import http
from debug_devices_mcp.ui.routes import error_response, json_response, monitor_of, until_closing
from debug_devices_mcp.webcam import WebcamError
from debug_devices_mcp.webcam_stream import CropMissingError, WebcamStream, crop_jpeg

if TYPE_CHECKING:
    from debug_devices_mcp.ui.monitor import Monitor

BAD_REQUEST = 400
NOT_FOUND = 404
CONFLICT = 409
SERVICE_UNAVAILABLE = 503
NO_STREAM = "the webcam stream is off"
NO_CROP = "no crop box: set the crop box on the page (this stream sends only the crop box, never the whole frame)"
# The page stream waits this long per frame, then tries again (ffmpeg can restart).
FRAME_WAIT = timedelta(seconds=5)


def mjpeg_part(jpeg: bytes) -> bytes:
    header = f"--{http.MJPEG_BOUNDARY}\r\nContent-Type: {http.JPEG_MEDIA_TYPE}\r\nContent-Length: {len(jpeg)}\r\n\r\n"
    return header.encode() + jpeg + b"\r\n"


async def mjpeg_body(stream: WebcamStream) -> AsyncIterator[bytes]:
    async for frame in stream.frames(FRAME_WAIT):
        yield mjpeg_part(frame.jpeg)


async def viewer_body(monitor: Monitor, stream: WebcamStream) -> AsyncGenerator[bytes]:
    """A page watches: the webcam starts, and it stays on while the page reads. Another owner: its stream."""
    async with monitor.webcam_viewer():
        if monitor.webcam_owner is not None and monitor.shared is not None:
            async for chunk in monitor.shared.remote.stream():
                yield chunk
            return
        async for part in mjpeg_body(stream):
            yield part


async def crop_body(monitor: Monitor, stream: WebcamStream) -> AsyncGenerator[bytes]:
    """Only the crop box of each frame: the full frame never leaves the server here. It follows a crop change and
    ends when the crop box is cleared. Another owner: its frames with its own crop box, or the end (never a whole
    frame and never the local webcam instead)."""
    async with monitor.webcam_viewer():
        if monitor.webcam_owner is not None and monitor.shared is not None:
            try:
                while True:
                    yield mjpeg_part(await monitor.shared.remote_cropped_jpeg())
            except WebcamError:
                return
        async for frame in stream.frames(FRAME_WAIT):
            crop = monitor.crop()
            if crop is None:
                return
            try:
                jpeg = await asyncio.to_thread(crop_jpeg, frame.jpeg, crop)
            except OSError, ValueError:
                continue
            yield mjpeg_part(jpeg)


async def get_crop_stream(request: Request) -> Response:
    """The meter picture of the full-screen phone view: only the crop box, smaller over a slow link."""
    monitor = monitor_of(request)
    if monitor.stream is None:
        return error_response(NO_STREAM, NOT_FOUND)
    if monitor.crop() is None and monitor.webcam_owner is None:
        return error_response(NO_CROP, CONFLICT)
    body = until_closing(crop_body(monitor, monitor.stream), monitor.closing)
    return StreamingResponse(body, media_type=http.MJPEG_MEDIA_TYPE, headers=http.NO_CACHE)


async def get_stream(request: Request) -> Response:
    """The live view. When another monitor owns the webcam, this page shows the stream of that monitor."""
    monitor = monitor_of(request)
    if monitor.stream is None:
        return error_response(NO_STREAM, NOT_FOUND)
    body = until_closing(viewer_body(monitor, monitor.stream), monitor.closing)
    return StreamingResponse(body, media_type=http.MJPEG_MEDIA_TYPE, headers=http.NO_CACHE)


class FrameQuery(BaseModel):
    # True: the next frame with the crop of this monitor. Other MCP processes read the webcam this way.
    cropped: bool = False


async def get_frame(request: Request) -> Response:
    """The latest full frame. With `cropped=true`, the next frame, cropped."""
    monitor = monitor_of(request)
    stream = monitor.stream
    if stream is None:
        return error_response(NO_STREAM, NOT_FOUND)
    try:
        query = FrameQuery.model_validate(dict(request.query_params))
    except ValidationError as exc:
        return error_response(str(exc), BAD_REQUEST)
    if not query.cropped:
        return owned_elsewhere(monitor) or latest_frame(stream)
    # Another MCP process takes a frame: this counts as a use, so the stream starts and stays on.
    async with monitor.webcam_user():
        return owned_elsewhere(monitor) or await cropped_frame(stream)


def owned_elsewhere(monitor: Monitor) -> Response | None:
    """Never pass frames on from a third monitor: the caller must ask the owner."""
    if monitor.webcam_owner is None:
        return None
    return error_response(f"the monitor at {monitor.webcam_owner} owns the webcam", SERVICE_UNAVAILABLE)


async def cropped_frame(stream: WebcamStream) -> Response:
    """Only the crop box: a crop box cleared while the request waits for the frame gives 409, never the whole frame
    (N99)."""
    try:
        jpeg = await stream.capture_cropped_jpeg()
    except CropMissingError as exc:
        return error_response(str(exc), CONFLICT)
    except WebcamError as exc:
        return error_response(str(exc), SERVICE_UNAVAILABLE)
    return Response(jpeg, media_type=http.JPEG_MEDIA_TYPE, headers=http.NO_CACHE)


def latest_frame(stream: WebcamStream) -> Response:
    if stream.latest is None:
        return error_response("no webcam frame yet", SERVICE_UNAVAILABLE)
    return Response(stream.latest.jpeg, media_type=http.JPEG_MEDIA_TYPE, headers=http.NO_CACHE)


async def get_info(request: Request) -> JSONResponse:
    info = await monitor_of(request).webcam_info()
    if info is None:
        return error_response(NO_STREAM, NOT_FOUND)
    return json_response(info)


routes = [
    Route("/api/webcam/stream.mjpg", get_stream, methods=["GET"]),
    Route("/api/webcam/crop.mjpg", get_crop_stream, methods=["GET"]),
    Route("/api/webcam/frame.jpg", get_frame, methods=["GET"]),
    Route("/api/webcam/info", get_info, methods=["GET"]),
]
