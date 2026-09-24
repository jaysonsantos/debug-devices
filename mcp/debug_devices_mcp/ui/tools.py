"""MCP tools of the monitor: open the page, and start or stop the whole bench in one call."""

from collections.abc import Awaitable, Callable
from enum import StrEnum

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel

from debug_devices_mcp.ui.constants import defaults, tools
from debug_devices_mcp.ui.events import truncate
from debug_devices_mcp.ui.monitor import Monitor


class StepStatus(StrEnum):
    OK = "ok"
    ERROR = "error"
    SKIPPED = "skipped"


class BenchStep(BaseModel):
    name: str
    status: StepStatus
    detail: str


class MonitorOpenResult(BaseModel):
    url: str
    opened_browser: bool
    browser: str | None


class BenchResult(BaseModel):
    """One status for the user: the page URL and each step."""

    url: str | None
    opened_browser: bool
    steps: list[BenchStep]


class StepName(StrEnum):
    PAGE = "page"
    WEBCAM = "webcam"
    PHONE = "phone"
    BOARD = "board"


type StepAction = Callable[[], Awaitable[str]]


async def run_step(name: StepName, enabled: bool, action: StepAction) -> BenchStep:
    """Run one step. A failure is a result, not an exception: the next steps still run."""
    if not enabled:
        return BenchStep(name=name, status=StepStatus.SKIPPED, detail="not asked")
    try:
        detail = await action()
    except Exception as exc:
        return BenchStep(name=name, status=StepStatus.ERROR, detail=truncate(str(exc) or type(exc).__name__))
    return BenchStep(name=name, status=StepStatus.OK, detail=detail)


class ToolFailedError(Exception):
    """A nested tool call returned an error result."""


async def nested_tool(monitor: Monitor, name: str, arguments: dict[str, object]) -> str:
    call, result = await monitor.call_nested(name, arguments)
    if result.is_error:
        raise ToolFailedError(call.summary)
    return call.summary


def register_monitor_tools(server: MCPServer, monitor: Monitor) -> None:
    @server.tool()
    async def monitor_open(open_browser: bool = True) -> MonitorOpenResult:
        """Start the local monitor page (live webcam, phone screen, controls, tool log) and return its URL.

        Tell the user the URL: the port can differ from 18766 when that port is busy. `open_browser` opens it in a
        new Firefox window. Nothing starts at process start; this tool or any other tool starts the page.
        """
        url = await monitor.ensure_page(auto_open=False)
        browser = await monitor.open_browser() if open_browser else None
        return MonitorOpenResult(url=url, opened_browser=browser is not None, browser=browser)

    @server.tool()
    async def bench_start(
        open_browser: bool = True,
        phone: bool = True,
        webcam: bool = True,
        board_path: str | None = None,
    ) -> BenchResult:
        """Start the bench in one call. Use it when the user says "start the bench".

        Follow bench_instructions (the user's instructions file): call it first in a new session.

        Steps: the monitor page (a Firefox window with `open_browser`), the webcam stream, phone_connect (with
        the phone screen), and board_open when `board_path` is given. Each step runs even when another one fails.
        The result has the page URL and the status of each step; tell the user both.
        """
        opened: list[str] = []

        async def start_page() -> str:
            url = await monitor.ensure_page(auto_open=False)
            if open_browser and (browser := await monitor.open_browser()) is not None:
                opened.append(browser)
            return url

        async def start_webcam() -> str:
            return await monitor.start_webcam_now()

        steps = [
            await run_step(StepName.PAGE, True, start_page),
            await run_step(StepName.WEBCAM, webcam, start_webcam),
            await run_step(StepName.PHONE, phone, lambda: nested_tool(monitor, tools.PHONE_CONNECT, {})),
            await run_step(
                StepName.BOARD,
                board_path is not None,
                lambda: nested_tool(monitor, tools.BOARD_OPEN, {"path": board_path}),
            ),
        ]
        return BenchResult(url=monitor.url, opened_browser=bool(opened), steps=steps)

    @server.tool()
    async def bench_stop() -> BenchResult:
        """Stop the bench. Use it when the user says "stop the bench".

        Stops the webcam stream (the camera is free for other programs), the phone screen and scrcpy, removes the
        adb forward of the phone camera, and stops the monitor page. The tool log stays in memory. Call
        bench_start or any tool to start again.
        """
        url = monitor.url

        async def stop_page() -> str:
            monitor.stop_page_soon()
            return f"stops in {defaults.PAGE_STOP_DELAY.total_seconds()} s"

        async def stop_webcam() -> str:
            await monitor.stop_webcam()
            return "stopped"

        async def stop_phone() -> str:
            await monitor.stop_phone()
            return "stopped"

        steps = [
            await run_step(StepName.WEBCAM, True, stop_webcam),
            await run_step(StepName.PHONE, True, stop_phone),
            await run_step(StepName.PAGE, url is not None, stop_page),
        ]
        return BenchResult(url=url, opened_browser=False, steps=steps)
