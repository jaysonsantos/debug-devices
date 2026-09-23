"""Phone controls on the page. Each one runs the MCP tool, so the activity log shows it."""

from typing import Any

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from debug_devices_mcp.ui.constants import http, tools
from debug_devices_mcp.ui.routes import error_response, json_response, monitor_of
from debug_devices_mcp.ui.views import RotationBody, TorchBody, ZoomBody

BAD_REQUEST = 400
NOT_FOUND = 404
BAD_GATEWAY = 502


async def run_tool(request: Request, name: str, arguments: dict[str, Any]) -> JSONResponse:
    """Run the tool and answer with the phone state. A tool error is a 502 with the tool message."""
    monitor = monitor_of(request)
    try:
        call, result = await monitor.call_from_ui(name, arguments)
    except ToolError as exc:
        return error_response(str(exc), BAD_GATEWAY)
    if result.is_error:
        return error_response(call.summary, BAD_GATEWAY)
    return json_response(monitor.bus.phone)


async def parse[T: BaseModel](request: Request, model: type[T]) -> T | JSONResponse:
    try:
        return model.model_validate_json(await request.body())
    except ValidationError as exc:
        return error_response(str(exc), BAD_REQUEST)


async def post_connect(request: Request) -> JSONResponse:
    return await run_tool(request, tools.PHONE_CONNECT, {})


async def post_status(request: Request) -> JSONResponse:
    return await run_tool(request, tools.PHONE_STATUS, {})


async def post_zoom(request: Request) -> JSONResponse:
    body = await parse(request, ZoomBody)
    if isinstance(body, JSONResponse):
        return body
    return await run_tool(request, tools.PHONE_ZOOM, body.model_dump(mode="json", exclude_none=True))


async def post_torch(request: Request) -> JSONResponse:
    body = await parse(request, TorchBody)
    if isinstance(body, JSONResponse):
        return body
    return await run_tool(request, tools.PHONE_TORCH, body.model_dump(mode="json"))


async def post_rotation(request: Request) -> JSONResponse:
    body = await parse(request, RotationBody)
    if isinstance(body, JSONResponse):
        return body
    return await run_tool(request, tools.PHONE_ROTATION, body.model_dump(mode="json", exclude_defaults=True))


async def post_snapshot(request: Request) -> JSONResponse:
    return await run_tool(request, tools.PHONE_SNAPSHOT, {})


async def get_last_snapshot(request: Request) -> Response:
    snapshot = monitor_of(request).last_snapshot
    if snapshot is None:
        return error_response("no phone snapshot yet", NOT_FOUND)
    return Response(snapshot, media_type=http.JPEG_MEDIA_TYPE, headers=http.NO_CACHE)


routes = [
    Route("/api/phone/connect", post_connect, methods=["POST"]),
    Route("/api/phone/status", post_status, methods=["POST"]),
    Route("/api/phone/zoom", post_zoom, methods=["POST"]),
    Route("/api/phone/torch", post_torch, methods=["POST"]),
    Route("/api/phone/rotation", post_rotation, methods=["POST"]),
    Route("/api/phone/snapshot", post_snapshot, methods=["POST"]),
    Route("/api/phone/snapshot.jpg", get_last_snapshot, methods=["GET"]),
]
