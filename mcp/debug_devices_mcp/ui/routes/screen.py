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


routes = [
    Route("/api/phone/screen", get_screen, methods=["GET"]),
]
