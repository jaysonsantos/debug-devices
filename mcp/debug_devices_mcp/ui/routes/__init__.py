"""HTTP routes of the monitor page, one resource per module."""

from typing import TYPE_CHECKING

from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import JSONResponse

from debug_devices_mcp.ui.views import ErrorView

if TYPE_CHECKING:
    from debug_devices_mcp.ui.monitor import Monitor


def monitor_of(request: Request) -> Monitor:
    return request.app.state.monitor


def json_response(model: BaseModel, status_code: int = 200) -> JSONResponse:
    return JSONResponse(model.model_dump(mode="json"), status_code=status_code)


def error_response(message: str, status_code: int) -> JSONResponse:
    return json_response(ErrorView(error=message), status_code)
