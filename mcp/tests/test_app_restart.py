"""The phone app restarted (a new app_start_id): every phone tool result says so until the agent's next
phone_snapshot, the registrations are stale with that reason, and the boxes and the tracking are cleared."""

from pathlib import Path
from uuid import uuid7

from mcp import Client
from mcp.types import CallToolResult

from debug_devices_mcp.app_restart import RESTART_NOTICE, AppRestartWatch, RestartNotice
from debug_devices_mcp.config import Settings
from debug_devices_mcp.phone_api import CameraStatus
from debug_devices_mcp.server import build_server

from .test_pointing import blank_photo, board_file, register, registered_services  # noqa: F401
from .test_server import STATUS

RESTART_ROTATION = 90


def texts(result: CallToolResult) -> list[str]:
    return [block.text for block in result.content if block.type == "text"]


def test_the_first_app_run_is_no_restart() -> None:
    watch = AppRestartWatch()
    first = CameraStatus.model_validate({**STATUS, "app_start_id": "a"})
    assert watch.restarted(first) is False
    assert watch.restarted(first) is False
    assert watch.restarted(CameraStatus.model_validate({**STATUS, "app_start_id": None})) is False
    assert watch.restarted(CameraStatus.model_validate({**STATUS, "app_start_id": "b"})) is True


async def test_an_app_restart_clears_and_tells(settings: Settings, board_file: Path) -> None:  # noqa: F811
    services, phone = registered_services(settings, blank_photo())
    heard: list[RestartNotice | None] = []

    async def listener(notice: RestartNotice | None) -> None:
        heard.append(notice)

    services.add_restart_listener(listener)
    server = build_server(services)
    async with Client(server) as client:
        registration = await register(client, board_file)
        pointed = await client.call_tool("phone_point_to", {"refdes": "U7301"})
        assert not pointed.is_error, pointed.content
        assert services.highlights or services.arrows
        before = await client.call_tool("phone_status", {})
        assert not any("restarted" in text for text in texts(before))

        phone.status.update(app_start_id=str(uuid7()), zoom_ratio=1.0, rotation_degrees=RESTART_ROTATION)
        status = await client.call_tool("phone_status", {})
        turn = services.orientation.transform(RESTART_ROTATION).turn_degrees
        message = RESTART_NOTICE.format(turn=turn, zoom=1.0)
        assert texts(status)[-1] == message
        assert services.highlights == []
        assert services.arrows == []
        assert services.pointing.tracker is None
        assert services.pointing.target is None
        old = services.board.registrations[registration["registration_id"]]
        assert old.stale is True
        refused = await client.call_tool("phone_point_to", {"refdes": "U7301"})
        assert refused.is_error
        assert "phone app restarted" in refused.content[0].text

        # A phone_snapshot from the page (no MCP context) keeps the notice for the agent.
        from_page = await server.call_tool("phone_snapshot", {"max_side": 0})
        assert isinstance(from_page, CallToolResult)
        assert texts(from_page)[-1] == message
        assert services.restart_notice is not None
        # The agent's next phone_snapshot carries it one last time; then it is gone.
        snapshot = await client.call_tool("phone_snapshot", {"max_side": 0})
        assert texts(snapshot)[-1] == message
        after = await client.call_tool("phone_status", {})
        assert not any("restarted" in text for text in texts(after))
    assert services.restart_notice is None
    assert heard[0] is not None
    assert heard[0].message == message
    assert heard[-1] is None
