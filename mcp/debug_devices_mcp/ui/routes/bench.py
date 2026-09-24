"""The "Start all" and "Stop all" buttons. They run bench_start and bench_stop, so the log shows them."""

from mcp.server.mcpserver.exceptions import ToolError
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from debug_devices_mcp.ui.constants import tools
from debug_devices_mcp.ui.routes import error_response, monitor_of

BAD_GATEWAY = 502


async def run_bench_tool(request: Request, name: str, arguments: dict[str, object]) -> JSONResponse:
    """Answer with the structured tool result: the page URL and the status of each step."""
    try:
        call, result = await monitor_of(request).call_from_ui(name, arguments)
    except ToolError as exc:
        return error_response(str(exc), BAD_GATEWAY)
    if result.is_error or result.structured_content is None:
        return error_response(call.summary, BAD_GATEWAY)
    return JSONResponse(result.structured_content)


async def post_start(request: Request) -> JSONResponse:
    # The page is open already: no new browser window.
    return await run_bench_tool(request, tools.BENCH_START, {"open_browser": False})


async def post_stop(request: Request) -> JSONResponse:
    return await run_bench_tool(request, tools.BENCH_STOP, {})


routes = [
    Route("/api/bench/start", post_start, methods=["POST"]),
    Route("/api/bench/stop", post_stop, methods=["POST"]),
]
