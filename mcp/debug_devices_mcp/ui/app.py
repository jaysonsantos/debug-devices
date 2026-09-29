"""The Starlette app of the monitor page. It listens on 127.0.0.1 only."""

from http import HTTPStatus
from typing import TYPE_CHECKING

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from debug_devices_mcp.ui.constants import INDEX_FILE, STATIC_DIR, http
from debug_devices_mcp.ui.origins import PageOrigins
from debug_devices_mcp.ui.routes import (
    bench,
    board,
    calls,
    devices,
    ingest,
    multimeter,
    phone,
    screen,
    settings,
    staged,
    state,
    webcam,
)

if TYPE_CHECKING:
    from debug_devices_mcp.ui.monitor import Monitor

BAD_HOST = "forbidden: this host name is not the monitor page (127.0.0.1, localhost, or --ui-allowed-origin)"
BAD_ORIGIN = "forbidden: a change from the origin {origin} (only 127.0.0.1, localhost, or --ui-allowed-origin)"


class LocalOnly:
    """Refuse requests for another host name (DNS rebinding) and writes from another site (CSRF). The error is JSON,
    so the page shows it."""

    def __init__(self, app: ASGIApp, origins: PageOrigins) -> None:
        self.app = app
        self.origins = origins

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            request = Request(scope)
            origin = request.headers.get(http.ORIGIN_HEADER)
            error = None
            if not self.origins.host_allowed(request.headers.get(http.HOST_HEADER)):
                error = BAD_HOST
            elif request.method not in http.SAFE_METHODS and origin is not None:
                error = None if self.origins.origin_allowed(origin) else BAD_ORIGIN.format(origin=origin)
            if error is not None:
                await JSONResponse({"error": error}, status_code=HTTPStatus.FORBIDDEN)(scope, receive, send)
                return
        await self.app(scope, receive, send)


async def index(_: Request) -> FileResponse:
    return FileResponse(STATIC_DIR / INDEX_FILE, headers=http.NO_CACHE)


def create_app(monitor: Monitor) -> Starlette:
    app = Starlette(
        routes=[
            Route("/", index, methods=["GET"]),
            *state.routes,
            *settings.routes,
            *webcam.routes,
            *phone.routes,
            *board.routes,
            *devices.routes,
            *staged.routes,
            *multimeter.routes,
            *bench.routes,
            *ingest.routes,
            *screen.routes,
            *calls.routes,
            Mount("/static", StaticFiles(directory=STATIC_DIR), name="static"),
        ]
    )
    app.state.monitor = monitor
    app.add_middleware(LocalOnly, origins=PageOrigins.of(monitor.options.allowed_origins))
    return app
