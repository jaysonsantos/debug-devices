"""The Starlette app of the monitor page. It listens on 127.0.0.1 only."""

from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, PlainTextResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from debug_devices_mcp.ui.constants import INDEX_FILE, STATIC_DIR, http
from debug_devices_mcp.ui.routes import bench, calls, multimeter, phone, screen, settings, state, webcam

if TYPE_CHECKING:
    from debug_devices_mcp.ui.monitor import Monitor

FORBIDDEN = 403


def allowed_host(value: str | None) -> bool:
    if not value:
        return False
    host = urlsplit(f"//{value}").hostname
    return host in http.ALLOWED_HOSTS


class LocalOnly:
    """Refuse requests for another host name (DNS rebinding) and writes from another site (CSRF)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            request = Request(scope)
            origin = request.headers.get(http.ORIGIN_HEADER)
            origin_host = urlsplit(origin).netloc if origin else None
            bad_origin = (
                request.method not in http.SAFE_METHODS and origin is not None and not allowed_host(origin_host)
            )
            if not allowed_host(request.headers.get(http.HOST_HEADER)) or bad_origin:
                await PlainTextResponse("forbidden", status_code=FORBIDDEN)(scope, receive, send)
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
            *multimeter.routes,
            *bench.routes,
            *screen.routes,
            *calls.routes,
            Mount("/static", StaticFiles(directory=STATIC_DIR), name="static"),
        ]
    )
    app.state.monitor = monitor
    app.add_middleware(LocalOnly)
    return app
