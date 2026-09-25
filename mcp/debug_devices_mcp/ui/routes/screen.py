"""The phone screen for the page: framed H.264 access units in one long HTTP response.

Wire format of each message: 4-byte big-endian payload length, 1 kind byte (0 config JSON, 1 key frame,
2 delta frame), then the payload. The page reads it with `fetch` and decodes the frames with WebCodecs.
"""

import asyncio
from collections.abc import AsyncGenerator
from datetime import timedelta

from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

from debug_devices_mcp.phone_screen import PhoneScreen
from debug_devices_mcp.screen_mjpeg import ScreenTranscoder
from debug_devices_mcp.ui.constants import http, screen
from debug_devices_mcp.ui.routes import error_response, monitor_of, until_closing

NOT_FOUND = 404
# How often the stream loop checks for a resync when no frame comes.
RESYNC_CHECK = timedelta(seconds=1)


async def screen_body(phone_screen: PhoneScreen) -> AsyncGenerator[bytes]:
    with phone_screen.subscribe() as subscriber:
        while True:
            if subscriber.resync:
                phone_screen.resync(subscriber)
            try:
                yield await asyncio.wait_for(subscriber.queue.get(), RESYNC_CHECK.total_seconds())
            except TimeoutError:
                continue


async def get_screen(request: Request) -> Response:
    monitor = monitor_of(request)
    if monitor.screen is None:
        return error_response("the phone screen is off (--no-phone-screen)", NOT_FOUND)
    return StreamingResponse(
        until_closing(screen_body(monitor.screen), monitor.closing), media_type=screen.MEDIA_TYPE, headers=http.NO_CACHE
    )


def mjpeg_part(jpeg: bytes) -> bytes:
    header = f"--{http.MJPEG_BOUNDARY}\r\nContent-Type: {http.JPEG_MEDIA_TYPE}\r\nContent-Length: {len(jpeg)}\r\n\r\n"
    return header.encode() + jpeg + b"\r\n"


async def mjpeg_body(transcoder: ScreenTranscoder) -> AsyncGenerator[bytes]:
    """ffmpeg runs while at least one fallback page reads this stream."""
    async with transcoder.viewer():
        async for jpeg in transcoder.frames():
            yield mjpeg_part(jpeg)


async def get_screen_mjpeg(request: Request) -> Response:
    """The phone screen as MJPEG, for browsers that cannot decode H.264 with WebCodecs."""
    monitor = monitor_of(request)
    if monitor.screen_mjpeg is None:
        return error_response("the phone screen is off (--no-phone-screen)", NOT_FOUND)
    body = until_closing(mjpeg_body(monitor.screen_mjpeg), monitor.closing)
    return StreamingResponse(body, media_type=http.MJPEG_MEDIA_TYPE, headers=http.NO_CACHE)


routes = [
    Route("/api/phone/screen", get_screen, methods=["GET"]),
    Route("/api/phone/screen.mjpg", get_screen_mjpeg, methods=["GET"]),
]
