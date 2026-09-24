"""The whole page state, loaded once when the page opens, and the SSE stream of changes."""

import asyncio
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from debug_devices_mcp.ui.constants import defaults, http
from debug_devices_mcp.ui.events import BusMessage
from debug_devices_mcp.ui.routes import json_response, monitor_of, until_closing
from debug_devices_mcp.ui.routes.settings import settings_view
from debug_devices_mcp.ui.views import StateView

if TYPE_CHECKING:
    from debug_devices_mcp.ui.monitor import Monitor

SSE_KEEPALIVE = ": keepalive\n\n"


def state_view(monitor: Monitor) -> StateView:
    return StateView(
        phone=monitor.bus.phone,
        settings=settings_view(monitor),
        webcam=monitor.stream.info() if monitor.stream is not None else None,
        calls=[call.to_event() for call in monitor.bus.calls()],
    )


async def get_state(request: Request) -> JSONResponse:
    return json_response(state_view(monitor_of(request)))


def sse_message(message: BusMessage) -> str:
    return f"event: {message.kind}\ndata: {message.data.model_dump_json()}\n\n"


async def get_events(request: Request) -> StreamingResponse:
    monitor = monitor_of(request)

    async def body() -> AsyncGenerator[str]:
        with monitor.bus.subscribe() as queue:
            yield SSE_KEEPALIVE
            while True:
                try:
                    message = await asyncio.wait_for(queue.get(), defaults.SSE_KEEPALIVE.total_seconds())
                except TimeoutError:
                    yield SSE_KEEPALIVE
                    continue
                yield sse_message(message)

    stream = until_closing(body(), monitor.closing)
    return StreamingResponse(stream, media_type=http.SSE_MEDIA_TYPE, headers=http.NO_CACHE)


async def get_whoami(request: Request) -> JSONResponse:
    """Tells other MCP processes that this port is a debug-devices monitor, and if it owns the webcam."""
    return json_response(monitor_of(request).identity())


routes = [
    Route("/api/whoami", get_whoami, methods=["GET"]),
    Route("/api/state", get_state, methods=["GET"]),
    Route("/api/events", get_events, methods=["GET"]),
]
