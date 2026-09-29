"""Staged captures on the page: capture (Space, C, or the Capture button), list, the phone photo and the meter crop
image of one capture, delete one, and clear. The page gets the list changes as `staged` events (ui/page_push.py).

A capture, a delete, and a clear accept only a same-origin request from the page (like the device actions): an
agent gets the captures only through `multimeter_read` and `staged_captures`.
"""

from http import HTTPStatus

from pydantic import BaseModel, field_validator
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from debug_devices_mcp.staged import Capturer, QueueFullError, is_capture_id, utc_now
from debug_devices_mcp.ui.constants import http, tools
from debug_devices_mcp.ui.events import CallSource
from debug_devices_mcp.ui.routes import error_response, json_response, monitor_of
from debug_devices_mcp.ui.routes.devices import refused
from debug_devices_mcp.ui.routes.phone import parse
from debug_devices_mcp.ui.staged_view import StagedView, staged_list

NO_CAPTURE = "staged captures need the MCP server of this page"
NOT_THE_PAGE = (
    "only the monitor page can capture, delete, or clear staged captures (a same-origin request, or an origin of "
    "--ui-allowed-origin)"
)
CAPTURE_ID_PARAM = "capture_id"
NOT_A_REQUEST_ID = "request_id must be a UUID in its standard form"


class StagedChange(BaseModel):
    changed: int


class CaptureBody(BaseModel):
    """The capture POST of the page. `request_id`: a new UUID for each key press. The browser can send the same POST
    again when a connection closes with no answer (a tunnel): that repeat gets the first answer, not a second capture
    (N97). None: a page from before this change."""

    request_id: str | None = None

    @field_validator("request_id")
    @classmethod
    def _uuid(cls, value: str | None) -> str | None:
        if value is not None and not is_capture_id(value):
            raise ValueError(NOT_A_REQUEST_ID)
        return value


async def capture_body(request: Request) -> CaptureBody | JSONResponse:
    if not await request.body():
        return CaptureBody()
    return await parse(request, CaptureBody)


async def get_staged(request: Request) -> JSONResponse:
    return json_response(await staged_list(monitor_of(request).staged))


async def post_capture(request: Request) -> JSONResponse:
    if (denied := refused(request, NOT_THE_PAGE)) is not None:
        return denied
    monitor = monitor_of(request)
    capturer = monitor.staged
    if capturer is None:
        return error_response(NO_CAPTURE, HTTPStatus.SERVICE_UNAVAILABLE)
    body = await capture_body(request)
    if isinstance(body, JSONResponse):
        return body
    if capturer.knows(body.request_id):
        # The same key press again (N97): the first answer, no second capture, and no second log row.
        return await repeat_answer(capturer, body.request_id)
    async with monitor.bus.record(tools.STAGED_CAPTURE, {}, CallSource.UI) as call:
        try:
            capture = await capturer.capture(body.request_id)
        except QueueFullError as exc:
            call.summary = str(exc)
            return error_response(str(exc), HTTPStatus.CONFLICT)
        call.summary = f"staged {capture.capture_id}: the photo and the meter reading follow in the list"
    return json_response(StagedView.of(capture, utc_now()), HTTPStatus.ACCEPTED)


async def repeat_answer(capturer: Capturer, request_id: str | None) -> JSONResponse:
    try:
        capture = await capturer.capture(request_id)
    except QueueFullError as exc:
        return error_response(str(exc), HTTPStatus.CONFLICT)
    return json_response(StagedView.of(capture, utc_now()), HTTPStatus.ACCEPTED)


async def get_photo(request: Request) -> Response:
    capturer = monitor_of(request).staged
    photo = await capturer.store.photo(request.path_params[CAPTURE_ID_PARAM]) if capturer is not None else None
    if photo is None:
        return error_response("no photo for this capture", HTTPStatus.NOT_FOUND)
    return Response(photo, media_type=http.JPEG_MEDIA_TYPE, headers=http.NO_CACHE)


async def get_meter_image(request: Request) -> Response:
    capturer = monitor_of(request).staged
    image = await capturer.store.meter_image(request.path_params[CAPTURE_ID_PARAM]) if capturer is not None else None
    if image is None:
        return error_response("no meter image for this capture", HTTPStatus.NOT_FOUND)
    return Response(image, media_type=http.JPEG_MEDIA_TYPE, headers=http.NO_CACHE)


async def delete_one(request: Request) -> JSONResponse:
    if (denied := refused(request, NOT_THE_PAGE)) is not None:
        return denied
    capturer = monitor_of(request).staged
    if capturer is None:
        return error_response(NO_CAPTURE, HTTPStatus.SERVICE_UNAVAILABLE)
    deleted = await capturer.store.delete(request.path_params[CAPTURE_ID_PARAM])
    return json_response(StagedChange(changed=int(deleted)))


async def delete_all(request: Request) -> JSONResponse:
    if (denied := refused(request, NOT_THE_PAGE)) is not None:
        return denied
    capturer = monitor_of(request).staged
    if capturer is None:
        return error_response(NO_CAPTURE, HTTPStatus.SERVICE_UNAVAILABLE)
    return json_response(StagedChange(changed=await capturer.store.clear()))


routes = [
    Route("/api/staged", get_staged, methods=["GET"]),
    Route("/api/staged", post_capture, methods=["POST"]),
    Route("/api/staged", delete_all, methods=["DELETE"]),
    Route(f"/api/staged/{{{CAPTURE_ID_PARAM}}}", delete_one, methods=["DELETE"]),
    Route(f"/api/staged/{{{CAPTURE_ID_PARAM}}}/photo.jpg", get_photo, methods=["GET"]),
    Route(f"/api/staged/{{{CAPTURE_ID_PARAM}}}/meter.jpg", get_meter_image, methods=["GET"]),
]
