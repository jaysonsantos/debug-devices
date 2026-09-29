"""The Devices part of the phone panel: list, select, disconnect, switch to Wi-Fi, pair, and connect (user actions)."""

from http import HTTPStatus

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from debug_devices_mcp.ui.constants import http
from debug_devices_mcp.ui.device_panel import ConnectBody, PairBody, SelectBody
from debug_devices_mcp.ui.origins import PageOrigins
from debug_devices_mcp.ui.routes import error_response, json_response, monitor_of
from debug_devices_mcp.ui.routes.phone import parse

NO_PANEL = "the Devices part needs the MCP server of this page"
NOT_THE_PAGE = "only the monitor page can change the phone (a same-origin request with an Origin header)"


def from_the_page(request: Request) -> bool:
    """A browser sends Origin with every POST; the page's own origin is this server, or an origin of
    --ui-allowed-origin (a tunnel). A local process without Origin (for example curl or an agent) cannot select, pair,
    connect, switch, or disconnect a device (S4 of QA round 4).
    """
    origins = PageOrigins.of(monitor_of(request).options.allowed_origins)
    return origins.from_the_page(request.headers.get(http.ORIGIN_HEADER), request.headers.get(http.HOST_HEADER))


def refused(request: Request, message: str = NOT_THE_PAGE) -> JSONResponse | None:
    return None if from_the_page(request) else error_response(message, HTTPStatus.FORBIDDEN)


async def get_devices(request: Request) -> JSONResponse:
    panel = monitor_of(request).device_panel
    if panel is None:
        return error_response(NO_PANEL, HTTPStatus.SERVICE_UNAVAILABLE)
    return json_response(await panel.devices())


async def post_select(request: Request) -> JSONResponse:
    if (denied := refused(request)) is not None:
        return denied
    panel = monitor_of(request).device_panel
    body = await parse(request, SelectBody)
    if isinstance(body, JSONResponse):
        return body
    if panel is None:
        return error_response(NO_PANEL, HTTPStatus.SERVICE_UNAVAILABLE)
    return json_response(await panel.select(body.serial))


async def post_disconnect(request: Request) -> JSONResponse:
    """Disconnect (also the old Clear selection route): always works, also when the device is gone."""
    if (denied := refused(request)) is not None:
        return denied
    panel = monitor_of(request).device_panel
    if panel is None:
        return error_response(NO_PANEL, HTTPStatus.SERVICE_UNAVAILABLE)
    return json_response(await panel.disconnect())


async def post_wifi(request: Request) -> JSONResponse:
    if (denied := refused(request)) is not None:
        return denied
    panel = monitor_of(request).device_panel
    body = await parse(request, SelectBody)
    if isinstance(body, JSONResponse):
        return body
    if panel is None:
        return error_response(NO_PANEL, HTTPStatus.SERVICE_UNAVAILABLE)
    return json_response(await panel.switch_to_wifi(body.serial))


async def post_pair(request: Request) -> JSONResponse:
    if (denied := refused(request)) is not None:
        return denied
    panel = monitor_of(request).device_panel
    body = await parse(request, PairBody)
    if isinstance(body, JSONResponse):
        return body
    if panel is None:
        return error_response(NO_PANEL, HTTPStatus.SERVICE_UNAVAILABLE)
    return json_response(await panel.pair(body))


async def post_connect(request: Request) -> JSONResponse:
    if (denied := refused(request)) is not None:
        return denied
    panel = monitor_of(request).device_panel
    body = await parse(request, ConnectBody)
    if isinstance(body, JSONResponse):
        return body
    if panel is None:
        return error_response(NO_PANEL, HTTPStatus.SERVICE_UNAVAILABLE)
    return json_response(await panel.connect(body.address))


routes = [
    Route("/api/devices", get_devices, methods=["GET"]),
    Route("/api/devices/select", post_select, methods=["POST"]),
    Route("/api/devices/clear", post_disconnect, methods=["POST"]),
    Route("/api/devices/disconnect", post_disconnect, methods=["POST"]),
    Route("/api/devices/wifi", post_wifi, methods=["POST"]),
    Route("/api/devices/pair", post_pair, methods=["POST"]),
    Route("/api/devices/connect", post_connect, methods=["POST"]),
]
