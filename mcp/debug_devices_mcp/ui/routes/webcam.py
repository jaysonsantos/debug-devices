"""The live webcam view: an MJPEG stream from the shared capture, and its size."""

import contextlib
from collections.abc import AsyncIterator
from datetime import timedelta

from pydantic import BaseModel, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from debug_devices_mcp.remote_webcam import RemoteUnavailableError
from debug_devices_mcp.ui.constants import http
from debug_devices_mcp.ui.routes import error_response, json_response, monitor_of
from debug_devices_mcp.webcam import WebcamError
from debug_devices_mcp.webcam_stream import StreamInfo, WebcamStream

BAD_REQUEST = 400
NOT_FOUND = 404
SERVICE_UNAVAILABLE = 503
NO_STREAM = "the webcam stream is off"
# The page stream waits this long per frame, then tries again (ffmpeg can restart).
FRAME_WAIT = timedelta(seconds=5)


def mjpeg_part(jpeg: bytes) -> bytes:
    header = f"--{http.MJPEG_BOUNDARY}\r\nContent-Type: {http.JPEG_MEDIA_TYPE}\r\nContent-Length: {len(jpeg)}\r\n\r\n"
    return header.encode() + jpeg + b"\r\n"


async def mjpeg_body(stream: WebcamStream) -> AsyncIterator[bytes]:
    async for frame in stream.frames(FRAME_WAIT):
        yield mjpeg_part(frame.jpeg)


async def get_stream(request: Request) -> Response:
    """The live view. When another monitor owns the webcam, this page shows the stream of that monitor."""
    monitor = monitor_of(request)
    if monitor.webcam_owner is not None and monitor.shared is not None:
        body = monitor.shared.remote.stream()
        return StreamingResponse(body, media_type=http.MJPEG_MEDIA_TYPE, headers=http.NO_CACHE)
    if monitor.stream is None:
        return error_response(NO_STREAM, NOT_FOUND)
    return StreamingResponse(mjpeg_body(monitor.stream), media_type=http.MJPEG_MEDIA_TYPE, headers=http.NO_CACHE)


class FrameQuery(BaseModel):
    # True: the next frame with the crop of this monitor. Other MCP processes read the webcam this way.
    cropped: bool = False


async def get_frame(request: Request) -> Response:
    """The latest full frame. With `cropped=true`, the next frame, cropped."""
    monitor = monitor_of(request)
    stream = monitor.stream
    if stream is None:
        return error_response(NO_STREAM, NOT_FOUND)
    if monitor.webcam_owner is not None:
        return error_response(f"the monitor at {monitor.webcam_owner} owns the webcam", SERVICE_UNAVAILABLE)
    try:
        query = FrameQuery.model_validate(dict(request.query_params))
    except ValidationError as exc:
        return error_response(str(exc), BAD_REQUEST)
    return await cropped_frame(stream) if query.cropped else latest_frame(stream)


async def cropped_frame(stream: WebcamStream) -> Response:
    try:
        jpeg = await stream.capture_jpeg()
    except WebcamError as exc:
        return error_response(str(exc), SERVICE_UNAVAILABLE)
    return Response(jpeg, media_type=http.JPEG_MEDIA_TYPE, headers=http.NO_CACHE)


def latest_frame(stream: WebcamStream) -> Response:
    if stream.latest is None:
        return error_response("no webcam frame yet", SERVICE_UNAVAILABLE)
    return Response(stream.latest.jpeg, media_type=http.JPEG_MEDIA_TYPE, headers=http.NO_CACHE)


async def get_info(request: Request) -> JSONResponse:
    monitor = monitor_of(request)
    if monitor.stream is None:
        return error_response(NO_STREAM, NOT_FOUND)
    info = monitor.stream.info()
    if monitor.webcam_owner is not None and monitor.shared is not None:
        with contextlib.suppress(RemoteUnavailableError, ValidationError):
            return json_response(StreamInfo.model_validate_json(await monitor.shared.remote.info()))
    if monitor.webcam_owner is not None:
        info = info.model_copy(
            update={"error": f"The monitor at {monitor.webcam_owner} owns the webcam. This process uses its frames."}
        )
    return json_response(info)


routes = [
    Route("/api/webcam/stream.mjpg", get_stream, methods=["GET"]),
    Route("/api/webcam/frame.jpg", get_frame, methods=["GET"]),
    Route("/api/webcam/info", get_info, methods=["GET"]),
]
