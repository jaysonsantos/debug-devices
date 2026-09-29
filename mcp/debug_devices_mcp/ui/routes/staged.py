"""Staged captures on the page: capture (Space, C, or the Capture button), list, one photo, delete one, and clear.

A capture, a delete, and a clear accept only a same-origin request from the page (like the device actions): an
agent gets the captures only through `multimeter_read` and `staged_captures`.
"""

from datetime import datetime
from http import HTTPStatus

from pydantic import AwareDatetime, BaseModel
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from debug_devices_mcp.staged import MAX_STAGED, STAGED_TTL, QueueFullError, StagedCapture, StagedState, utc_now
from debug_devices_mcp.ui.constants import http, tools
from debug_devices_mcp.ui.events import CallSource
from debug_devices_mcp.ui.routes import error_response, json_response, monitor_of
from debug_devices_mcp.ui.routes.devices import refused

NO_CAPTURE = "staged captures need the MCP server of this page"
NOT_THE_PAGE = "only the monitor page can capture, delete, or clear staged captures (a same-origin request)"
CAPTURE_ID_PARAM = "capture_id"
SECONDS_PER_MINUTE = 60


class StagedView(BaseModel):
    """One capture as the page shows it."""

    capture_id: str
    captured_at: AwareDatetime
    age_s: float
    state: StagedState
    origin: str
    has_photo: bool
    # For example "4.98 V" and "confirmed"; None while the reading runs or without a meter part.
    meter_text: str | None = None
    meter_status: str | None = None
    bench_notice: str | None = None
    notes: list[str] = []

    @classmethod
    def of(cls, capture: StagedCapture, now: datetime) -> StagedView:
        meter = capture.meter
        return cls(
            capture_id=capture.capture_id,
            captured_at=capture.captured_at,
            age_s=capture.age_seconds(now),
            state=capture.state,
            origin=capture.origin,
            has_photo=capture.photo is not None,
            meter_text=f"{meter.display_text} {meter.unit}".strip() if meter is not None else None,
            meter_status=str(meter.status) if meter is not None else None,
            bench_notice=meter.bench_notice if meter is not None else None,
            notes=capture.notes,
        )


class StagedList(BaseModel):
    captures: list[StagedView]
    limit: int = MAX_STAGED
    ttl_minutes: int = int(STAGED_TTL.total_seconds() // SECONDS_PER_MINUTE)


class StagedChange(BaseModel):
    changed: int


async def get_staged(request: Request) -> JSONResponse:
    capturer = monitor_of(request).staged
    if capturer is None:
        return json_response(StagedList(captures=[]))
    now = utc_now()
    return json_response(StagedList(captures=[StagedView.of(item, now) for item in await capturer.store.list()]))


async def post_capture(request: Request) -> JSONResponse:
    if (denied := refused(request, NOT_THE_PAGE)) is not None:
        return denied
    monitor = monitor_of(request)
    if monitor.staged is None:
        return error_response(NO_CAPTURE, HTTPStatus.SERVICE_UNAVAILABLE)
    async with monitor.bus.record(tools.STAGED_CAPTURE, {}, CallSource.UI) as call:
        try:
            capture = await monitor.staged.capture()
        except QueueFullError as exc:
            call.summary = str(exc)
            return error_response(str(exc), HTTPStatus.CONFLICT)
        call.summary = f"staged {capture.capture_id}: the photo and the meter reading follow in the list"
    return json_response(StagedView.of(capture, utc_now()), HTTPStatus.ACCEPTED)


async def get_photo(request: Request) -> Response:
    capturer = monitor_of(request).staged
    photo = await capturer.store.photo(request.path_params[CAPTURE_ID_PARAM]) if capturer is not None else None
    if photo is None:
        return error_response("no photo for this capture", HTTPStatus.NOT_FOUND)
    return Response(photo, media_type=http.JPEG_MEDIA_TYPE, headers=http.NO_CACHE)


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
]
