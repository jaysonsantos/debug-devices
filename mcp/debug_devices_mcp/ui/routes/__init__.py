"""HTTP routes of the monitor page, one resource per module."""

import asyncio
import contextlib
from collections.abc import AsyncGenerator, AsyncIterator
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


async def until_closing[T](source: AsyncGenerator[T], closing: asyncio.Event) -> AsyncIterator[T]:
    """Pass on the items of a long stream (SSE, MJPEG, phone screen), and end it at once when the page stops.

    Without this, a stream that waits for its next item keeps uvicorn busy after the stop, and uvicorn cancels it
    with an error after its graceful time.
    """
    closed = asyncio.ensure_future(closing.wait())
    try:
        while True:
            item = asyncio.ensure_future(anext(source))
            await asyncio.wait({item, closed}, return_when=asyncio.FIRST_COMPLETED)
            if not item.done():
                item.cancel()
                with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
                    await item
                return
            try:
                yield item.result()
            except StopAsyncIteration:
                return
    finally:
        closed.cancel()
        await source.aclose()
