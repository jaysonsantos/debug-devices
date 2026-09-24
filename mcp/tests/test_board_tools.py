"""Board MCP tools through the in-process MCP client, with a fake obv-dump."""

import base64
import io
import json
from datetime import timedelta
from pathlib import Path

import pytest
from mcp import Client
from mcp.types import CallToolResult, ImageContent, TextContent
from PIL import Image

from debug_devices_mcp.board.loader import LoaderOptions
from debug_devices_mcp.board.tools import BoardSession
from debug_devices_mcp.config import Settings
from debug_devices_mcp.process import CommandResult
from debug_devices_mcp.server import build_server

from .conftest import FakeRunner
from .test_board import fixture
from .test_server import FakePhone, make_services, no_vision

BOARD_TOOLS = {
    "board_open",
    "board_find_part",
    "board_part_pins",
    "board_find_net",
    "board_parts_near",
    "board_render",
}


def server_with(settings: Settings, stdout: bytes, returncode: int = 0) -> tuple[object, FakeRunner]:
    services = make_services(settings, FakePhone(), no_vision())
    runner = FakeRunner(lambda _: CommandResult(returncode=returncode, stdout=stdout, stderr=b""))
    services.board = BoardSession.create(runner, LoaderOptions(dump_bin="obv-dump", timeout=timedelta(seconds=5)))
    return build_server(services), runner


def text(result: CallToolResult) -> str:
    return next(block for block in result.content if isinstance(block, TextContent)).text


@pytest.fixture
def board_file(tmp_path: Path) -> Path:
    path = tmp_path / "tiny.cad"
    path.write_text("the fake obv-dump does not read this")
    return path


async def test_tools_are_listed_and_need_a_board(settings: Settings) -> None:
    server, _ = server_with(settings, fixture("tiny.json"))
    async with Client(server) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        result = await client.call_tool("board_find_part", {"query": "U1"})

    assert names >= BOARD_TOOLS
    assert result.is_error
    assert "board_open" in text(result)


async def test_open_find_and_pins(settings: Settings, board_file: Path) -> None:
    server, runner = server_with(settings, fixture("tiny.json"))
    async with Client(server) as client:
        opened = await client.call_tool("board_open", {"path": str(board_file)})
        again = await client.call_tool("board_open", {"path": str(board_file)})
        found = await client.call_tool("board_find_part", {"query": "TPS62130*"})
        pins = await client.call_tool("board_part_pins", {"refdes": "u1"})
        missing = await client.call_tool("board_part_pins", {"refdes": "Q9"})

    summary = opened.structured_content
    assert summary is not None
    assert (summary["parts"], summary["pins"], summary["nets"], summary["nails"]) == (6, 13, 5, 3)
    assert summary["test_points"] == 4
    assert summary["width_mm"] == 50.8
    assert summary["part_sides"] == {"top": 3, "bottom": 2, "both": 1}
    assert again.structured_content is not None
    assert again.structured_content["cached"] is True
    assert len(runner.calls) == 1
    assert found.structured_content is not None
    assert [part["name"] for part in found.structured_content["parts"]] == ["U1", "U2"]
    assert pins.structured_content is not None
    assert [pin["name"] for pin in pins.structured_content["pins"]] == ["VIN", "GND", "VOUT", "EN"]
    assert pins.structured_content["pins"][0]["position"] == {"x": 25.4, "y": 12.7}
    assert missing.is_error


async def test_find_net_and_parts_near(settings: Settings, board_file: Path) -> None:
    server, _ = server_with(settings, fixture("tiny.json"))
    async with Client(server) as client:
        await client.call_tool("board_open", {"path": str(board_file)})
        net = await client.call_tool("board_find_net", {"query": "pp1v8"})
        no_net = await client.call_tool("board_find_net", {"query": "VCORE"})
        near = await client.call_tool("board_parts_near", {"refdes": "U1", "radius_mm": 15, "side": "top"})
        near_point = await client.call_tool("board_parts_near", {"x_mm": 2.54, "y_mm": 3.81, "radius_mm": 2})
        near_nothing = await client.call_tool("board_parts_near", {"radius_mm": 2})

    assert net.structured_content is not None
    (report,) = net.structured_content["nets"]
    assert report["name"] == "PP1V8"
    assert {part["name"] for part in report["parts"]} == {"U1", "C1", "TP1"}
    u1 = next(part for part in report["parts"] if part["name"] == "U1")
    assert u1["pin_numbers"] == ["3"]
    assert u1["nearest_test_point"]["test_point"]["name"] == "TP1"
    assert no_net.is_error
    assert "*VCORE*" in text(no_net)
    assert near.structured_content is not None
    assert [item["part"]["name"] for item in near.structured_content["parts"]] == ["R1", "TP1"]
    assert near_point.structured_content is not None
    assert [item["part"]["name"] for item in near_point.structured_content["parts"]] == ["J1"]
    assert near_nothing.is_error


async def test_render_returns_png_and_legend(settings: Settings, board_file: Path) -> None:
    server, _ = server_with(settings, fixture("tiny.json"))
    async with Client(server) as client:
        await client.call_tool("board_open", {"path": str(board_file)})
        whole = await client.call_tool("board_render", {"highlight_parts": ["U1"], "highlight_nets": ["GND"]})
        crop = await client.call_tool("board_render", {"crop_to_part": "U2", "max_side": 800})
        bad = await client.call_tool("board_render", {"crop_to_part": "Q9"})

    assert not whole.is_error, whole.content
    image = next(block for block in whole.content if isinstance(block, ImageContent))
    assert image.mime_type == "image/png"
    with Image.open(io.BytesIO(base64.b64decode(image.data))) as png:
        assert max(png.size) == 1568
    legend = whole.structured_content
    assert legend is not None
    assert legend["side"] == "top"
    assert legend["highlighted_nets"][0]["pins_drawn"] == 2  # GND on top: U1 pin 2 and J1 pin 2 (both); not C1
    assert json.loads(text(whole)) == legend
    assert crop.structured_content is not None
    # U2 is on the bottom, so the crop uses the bottom side, mirrored.
    assert crop.structured_content["side"] == "bottom"
    assert crop.structured_content["mirrored_x"] is True
    assert max(crop.structured_content["width_px"], crop.structured_content["height_px"]) == 800
    assert bad.is_error


async def test_dump_error_becomes_tool_error(settings: Settings, board_file: Path) -> None:
    server, _ = server_with(settings, fixture("error_parse_failed.json"), returncode=1)
    async with Client(server) as client:
        result = await client.call_tool("board_open", {"path": str(board_file)})

    assert result.is_error
    assert "parse_failed" in text(result)
    assert "$SHAPES" in text(result)
