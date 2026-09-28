"""The Devices part of the phone panel: list, select, clear, switch to Wi-Fi, pair, and connect (user actions)."""

from http import HTTPStatus

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from debug_devices_mcp.ui.device_panel import ConnectBody, PairBody, SelectBody
from debug_devices_mcp.ui.routes import error_response, json_response, monitor_of
from debug_devices_mcp.ui.routes.phone import parse

NO_PANEL = "the Devices part needs the MCP server of this page"


async def get_devices(request: Request) -> JSONResponse:
    panel = monitor_of(request).device_panel
    if panel is None:
        return error_response(NO_PANEL, HTTPStatus.SERVICE_UNAVAILABLE)
    return json_response(await panel.devices())


async def post_select(request: Request) -> JSONResponse:
    panel = monitor_of(request).device_panel
    body = await parse(request, SelectBody)
    if isinstance(body, JSONResponse):
        return body
    if panel is None:
        return error_response(NO_PANEL, HTTPStatus.SERVICE_UNAVAILABLE)
    return json_response(await panel.select(body.serial))


async def post_clear(request: Request) -> JSONResponse:
    panel = monitor_of(request).device_panel
    if panel is None:
        return error_response(NO_PANEL, HTTPStatus.SERVICE_UNAVAILABLE)
    return json_response(await panel.clear())


async def post_wifi(request: Request) -> JSONResponse:
    panel = monitor_of(request).device_panel
    body = await parse(request, SelectBody)
    if isinstance(body, JSONResponse):
        return body
    if panel is None:
        return error_response(NO_PANEL, HTTPStatus.SERVICE_UNAVAILABLE)
    return json_response(await panel.switch_to_wifi(body.serial))


async def post_pair(request: Request) -> JSONResponse:
    panel = monitor_of(request).device_panel
    body = await parse(request, PairBody)
    if isinstance(body, JSONResponse):
        return body
    if panel is None:
        return error_response(NO_PANEL, HTTPStatus.SERVICE_UNAVAILABLE)
    return json_response(await panel.pair(body))


async def post_connect(request: Request) -> JSONResponse:
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
    Route("/api/devices/clear", post_clear, methods=["POST"]),
    Route("/api/devices/wifi", post_wifi, methods=["POST"]),
    Route("/api/devices/pair", post_pair, methods=["POST"]),
    Route("/api/devices/connect", post_connect, methods=["POST"]),
]
