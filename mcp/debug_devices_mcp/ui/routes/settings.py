"""Settings that the user changes on the page: vision model, webcam warm-up, and webcam crop."""

from typing import TYPE_CHECKING

from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from debug_devices_mcp.ui.routes import error_response, json_response, monitor_of
from debug_devices_mcp.ui.settings import SettingsView, UiSettings

if TYPE_CHECKING:
    from debug_devices_mcp.ui.monitor import Monitor

BAD_REQUEST = 400


def settings_view(monitor: Monitor) -> SettingsView:
    return monitor.settings_view()


async def get_settings(request: Request) -> JSONResponse:
    return json_response(settings_view(monitor_of(request)))


async def put_settings(request: Request) -> JSONResponse:
    """Replace the saved settings. A null field goes back to the start value."""
    monitor = monitor_of(request)
    try:
        saved = UiSettings.model_validate_json(await request.body())
    except ValidationError as exc:
        return error_response(str(exc), BAD_REQUEST)
    before = monitor.effective.webcam_crop
    await monitor.save_settings(saved)
    await monitor.log_crop_change(before)
    return json_response(settings_view(monitor))


async def delete_crop(request: Request) -> JSONResponse:
    monitor = monitor_of(request)
    before = monitor.effective.webcam_crop
    await monitor.clear_crop()
    await monitor.log_crop_change(before)
    return json_response(settings_view(monitor))


routes = [
    Route("/api/settings", get_settings, methods=["GET"]),
    Route("/api/settings", put_settings, methods=["PUT"]),
    Route("/api/settings/crop", delete_crop, methods=["DELETE"]),
]
