"""The "Read multimeter" button. It runs the MCP tool, so the log shows the sent image and the reading."""

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from debug_devices_mcp.multimeter import MeterSource
from debug_devices_mcp.ui.constants import tools
from debug_devices_mcp.ui.routes import error_response, json_response, monitor_of

BAD_REQUEST = 400
BAD_GATEWAY = 502


class ReadBody(BaseModel):
    source: MeterSource = MeterSource.WEBCAM


async def post_read(request: Request) -> JSONResponse:
    """Answer with the recorded call: status, reading (in `details`), and the image references.

    The body `{"source": "webcam" | "phone"}` is optional. The tool returns its image, so the log shows the image
    that the model saw, also for the phone.
    """
    raw = await request.body()
    try:
        body = ReadBody.model_validate_json(raw) if raw else ReadBody()
    except ValidationError as exc:
        return error_response(str(exc), BAD_REQUEST)
    arguments = {"source": body.source.value, "include_image": True}
    try:
        call, result = await monitor_of(request).call_from_ui(tools.MULTIMETER_READ, arguments)
    except ToolError as exc:
        return error_response(str(exc), BAD_GATEWAY)
    if result.is_error:
        return error_response(call.summary, BAD_GATEWAY)
    return json_response(call.to_event())


routes = [
    Route("/api/multimeter/read", post_read, methods=["POST"]),
]
