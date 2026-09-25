"""Tool calls of other MCP servers (secondaries): their events and images come here, so this page shows them all.

Only a request with the token of this page gets in (a file with mode 600, see `ui/forward.py`). The LocalOnly
middleware also checks the host and the origin, as for every route.
"""

import secrets
from uuid import UUID

from pydantic import BaseModel, ValidationError
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from debug_devices_mcp.ui.constants import http, ingest
from debug_devices_mcp.ui.forward import IngestCall
from debug_devices_mcp.ui.routes import error_response, monitor_of

BAD_REQUEST = 400
FORBIDDEN = 403
NOT_FOUND = 404
TOO_LARGE = 413
UNSUPPORTED_MEDIA_TYPE = 415
NO_CONTENT = 204


class ImageQuery(BaseModel):
    origin: str
    label: str


def token_ok(request: Request) -> bool:
    expected = monitor_of(request).ingest_token
    given = request.headers.get(ingest.TOKEN_HEADER)
    return expected is not None and given is not None and secrets.compare_digest(given, expected)


async def post_call(request: Request) -> Response:
    if not token_ok(request):
        return error_response("a valid ingest token is required", FORBIDDEN)
    body = await request.body()
    if len(body) > ingest.MAX_EVENT_BYTES:
        return error_response("the event is too large", TOO_LARGE)
    try:
        data = IngestCall.model_validate_json(body)
    except ValidationError as exc:
        return error_response(str(exc), BAD_REQUEST)
    monitor_of(request).bus.ingest(data.event, data.origin)
    return Response(status_code=NO_CONTENT)


async def post_image(request: Request) -> Response:
    if not token_ok(request):
        return error_response("a valid ingest token is required", FORBIDDEN)
    media_type = request.headers.get(http.CONTENT_TYPE_HEADER, "").split(";")[0].strip()
    if media_type not in ingest.IMAGE_MEDIA_TYPES:
        return error_response(f"send one of {sorted(ingest.IMAGE_MEDIA_TYPES)}", UNSUPPORTED_MEDIA_TYPE)
    try:
        query = ImageQuery.model_validate(dict(request.query_params))
    except ValidationError as exc:
        return error_response(str(exc), BAD_REQUEST)
    body = await request.body()
    if len(body) > ingest.MAX_IMAGE_BYTES:
        return error_response("the image is too large", TOO_LARGE)
    call_id: UUID = request.path_params["call_id"]
    if not monitor_of(request).bus.attach_ingested_image(query.origin, call_id, query.label, body):
        return error_response("send the call event first", NOT_FOUND)
    return Response(status_code=NO_CONTENT)


routes = [
    Route(ingest.CALLS_PATH, post_call, methods=["POST"]),
    Route("/api/ingest/calls/{call_id:uuid}/images", post_image, methods=["POST"]),
]
