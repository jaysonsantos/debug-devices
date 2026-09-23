"""Images that a tool call took or returned."""

from uuid import UUID

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from debug_devices_mcp.ui.constants import http
from debug_devices_mcp.ui.routes import error_response, monitor_of

NOT_FOUND = 404


async def get_call_image(request: Request) -> Response:
    call = monitor_of(request).bus.find_call(UUID(str(request.path_params["call_id"])))
    index: int = request.path_params["index"]
    if call is None or index >= len(call.images):
        return error_response("the image is not in the history anymore", NOT_FOUND)
    return Response(call.images[index].jpeg, media_type=http.JPEG_MEDIA_TYPE)


routes = [
    Route("/api/calls/{call_id:uuid}/images/{index:int}", get_call_image, methods=["GET"]),
]
