"""The Board panel: open a board, search a part or a net, highlight it on the camera, and register the snapshot."""

from http import HTTPStatus

from mcp.server.mcpserver.exceptions import ToolError
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from debug_devices_mcp.ui.board import NO_BOARD, OpenBody, RegisterBody, SearchBody, board_names
from debug_devices_mcp.ui.routes import error_response, json_response, monitor_of
from debug_devices_mcp.ui.routes.phone import parse

NO_PANEL = "the Board panel needs the MCP server of this page"


async def get_board(request: Request) -> JSONResponse:
    monitor = monitor_of(request)
    if monitor.board_panel is None:
        return error_response(NO_PANEL, HTTPStatus.SERVICE_UNAVAILABLE)
    return json_response(monitor.board_panel.view(monitor.remote_board()))


async def get_names(request: Request) -> JSONResponse:
    panel = monitor_of(request).board_panel
    if panel is None:
        return error_response(NO_PANEL, HTTPStatus.SERVICE_UNAVAILABLE)
    if panel.session.board is None:
        return error_response(NO_BOARD, HTTPStatus.CONFLICT)
    return json_response(board_names(panel.session.board))


async def post_open(request: Request) -> JSONResponse:
    monitor = monitor_of(request)
    body = await parse(request, OpenBody)
    if isinstance(body, JSONResponse):
        return body
    if monitor.board_panel is None:
        return error_response(NO_PANEL, HTTPStatus.SERVICE_UNAVAILABLE)
    try:
        await monitor.board_panel.open(body.path)
    except ToolError as exc:
        return error_response(str(exc), HTTPStatus.BAD_GATEWAY)
    return json_response(monitor.board_panel.view(monitor.remote_board()))


async def post_search(request: Request) -> JSONResponse:
    panel = monitor_of(request).board_panel
    body = await parse(request, SearchBody)
    if isinstance(body, JSONResponse):
        return body
    if panel is None:
        return error_response(NO_PANEL, HTTPStatus.SERVICE_UNAVAILABLE)
    try:
        return json_response(await panel.search(body.query))
    except ToolError as exc:
        return error_response(str(exc), HTTPStatus.BAD_GATEWAY)


async def post_register(request: Request) -> JSONResponse:
    panel = monitor_of(request).board_panel
    body = await parse(request, RegisterBody)
    if isinstance(body, JSONResponse):
        return body
    if panel is None:
        return error_response(NO_PANEL, HTTPStatus.SERVICE_UNAVAILABLE)
    try:
        return json_response(await panel.register(body))
    except ToolError as exc:
        return error_response(str(exc), HTTPStatus.BAD_GATEWAY)


routes = [
    Route("/api/board", get_board, methods=["GET"]),
    Route("/api/board/names", get_names, methods=["GET"]),
    Route("/api/board/open", post_open, methods=["POST"]),
    Route("/api/board/search", post_search, methods=["POST"]),
    Route("/api/board/register", post_register, methods=["POST"]),
]
