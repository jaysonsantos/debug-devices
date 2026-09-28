"""Tool calls of other MCP servers (secondaries): their events and images come here, so this page shows them all.

Only a request with the token of this page gets in (a file with mode 600, see `ui/forward.py`). The LocalOnly
middleware also checks the host and the origin, as for every route.
"""

import logging
import secrets
from uuid import UUID

from pydantic import BaseModel, ValidationError
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from debug_devices_mcp.ui.constants import http, ingest
from debug_devices_mcp.ui.forward import IngestCall, IngestOverlay
from debug_devices_mcp.ui.remote_screen import ScreenStart
from debug_devices_mcp.ui.routes import error_response, json_response, monitor_of

BAD_REQUEST = 400
logger = logging.getLogger(__name__)

FORBIDDEN = 403
NOT_SELECTED = "{serial} is not the phone that the user selected in this page: no adb command goes to it"
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


async def post_overlay(request: Request) -> Response:
    """The boxes and arrows of a secondary server: the page draws them on its snapshot with the origin."""
    if not token_ok(request):
        return error_response("a valid ingest token is required", FORBIDDEN)
    try:
        data = IngestOverlay.model_validate_json(await request.body())
    except ValidationError as exc:
        return error_response(str(exc), BAD_REQUEST)
    monitor_of(request).remote_overlay(data)
    return Response(status_code=NO_CONTENT)


async def post_screen_start(request: Request) -> Response:
    """A secondary connected the phone: stream it here, so its live tracking gets frames."""
    if not token_ok(request):
        return error_response("a valid ingest token is required", FORBIDDEN)
    try:
        data = ScreenStart.model_validate_json(await request.body())
    except ValidationError as exc:
        return error_response(str(exc), BAD_REQUEST)
    monitor = monitor_of(request)
    # adb commands go only to the phone that the user selected (N2 of QA round 6), also for a secondary's request.
    selected = monitor.selected_serial() if monitor.selected_serial is not None else ""
    if data.serial != selected:
        logger.warning(
            "a secondary server asked to stream %s, but the selected phone is %s: refused",
            data.serial,
            selected or "none",
        )
        return error_response(NOT_SELECTED.format(serial=data.serial), FORBIDDEN)
    return json_response(await monitor.start_screen_for(data.serial))


async def get_frame(request: Request) -> Response:
    """The newest phone screen frame (JPEG), or 204 when it is frame `after`. An `after` above the newest number
    comes from before a restart of this server (its counter starts at 0 again): the newest frame goes back
    (B-F6 of QA round 4)."""
    if not token_ok(request):
        return error_response("a valid ingest token is required", FORBIDDEN)
    try:
        after = int(request.query_params.get(ingest.AFTER_PARAM, "0"))
    except ValueError:
        return error_response(f"{ingest.AFTER_PARAM} must be a number", BAD_REQUEST)
    frame = monitor_of(request).scene_frame()
    if frame is None or frame[0] == after:
        return Response(status_code=NO_CONTENT)
    seq, jpeg = frame
    return Response(jpeg, media_type=http.JPEG_MEDIA_TYPE, headers={ingest.FRAME_SEQ_HEADER: str(seq)})


routes = [
    Route(ingest.CALLS_PATH, post_call, methods=["POST"]),
    Route(ingest.OVERLAY_PATH, post_overlay, methods=["POST"]),
    Route(ingest.SCREEN_START_PATH, post_screen_start, methods=["POST"]),
    Route(ingest.FRAME_PATH, get_frame, methods=["GET"]),
    Route("/api/ingest/calls/{call_id:uuid}/images", post_image, methods=["POST"]),
]
