"""Part identity claims (board/identity.py). Report: "P1: Check physical part identity".

Fixture: mcp/tests/fixtures/boardview/identity.json (synthetic; all names are invented).
"""

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from mcp.types import TextContent

from debug_devices_mcp.board.dump import BoardDump
from debug_devices_mcp.board.loader import LoaderOptions
from debug_devices_mcp.board.model import Board
from debug_devices_mcp.board.tools import BoardSession
from debug_devices_mcp.config import Settings
from debug_devices_mcp.process import CommandResult
from debug_devices_mcp.server import Services, build_server

from .conftest import FakeRunner
from .test_board import fixture
from .test_server import FakePhone, make_services, no_vision

# The fake photo: 10 px per mm, y down.
PX_PER_MM = 10.0
ORIGIN_PX = (100.0, 900.0)
PAIR_PARTS = ("J4", "Q12", "TP9", "C8850", "R10")
SIZE = {"photo_width_px": 1200, "photo_height_px": 1000}


def board() -> Board:
    return Board(BoardDump.model_validate_json(fixture("identity.json")), "sha")


def to_photo(name: str, dx_mm: float = 0.0) -> tuple[float, float]:
    center = board().parts[name].center
    return ORIGIN_PX[0] + PX_PER_MM * (center.x + dx_mm), ORIGIN_PX[1] - PX_PER_MM * center.y


def pairs(names: tuple[str, ...] = PAIR_PARTS) -> list[dict[str, Any]]:
    return [{"refdes": name, "x_px": to_photo(name)[0], "y_px": to_photo(name)[1]} for name in names]


def photo_id(result: Any) -> str:
    return json.loads(next(block for block in result.content if isinstance(block, TextContent)).text)["capture_id"]


class Bench:
    def __init__(self, settings: Settings, tmp_path: Path) -> None:
        self.services: Services = make_services(settings, FakePhone(), no_vision())
        runner = FakeRunner(lambda _: CommandResult(returncode=0, stdout=fixture("identity.json"), stderr=b""))
        options = LoaderOptions(dump_bin="obv-dump", timeout=timedelta(seconds=5))
        self.services.board = BoardSession.create(runner, options)
        self.server = build_server(self.services)
        self.board_file = tmp_path / "identity.cad"
        self.board_file.write_text("x")

    async def ready(self, client: Client, side: str = "top") -> tuple[str, str]:
        """Open the board, take a photo, register it. Returns the photo id and the registration id."""
        await client.call_tool("board_open", {"path": str(self.board_file)})
        photo = photo_id(await client.call_tool("phone_snapshot", {}))
        registered = await client.call_tool("board_register_photo", {"side": side, "pairs": pairs(), **SIZE})
        assert registered.structured_content is not None, registered.content
        return photo, registered.structured_content["registration_id"]


@pytest.fixture
def bench(settings: Settings, tmp_path: Path) -> Bench:
    return Bench(settings, tmp_path)


async def identify(client: Client, **arguments: object) -> dict[str, Any]:
    result = await client.call_tool("board_identify", arguments)
    assert result.structured_content is not None, result.content
    return result.structured_content


async def test_four_similar_coils_stay_candidates(bench: Bench) -> None:
    async with Client(bench.server) as client:
        photo, registration = await bench.ready(client)
        x, y = to_photo("L502")
        claim = await identify(client, photo_id=photo, registration_id=registration, x_px=x, y_px=y)

    assert claim["state"] == "candidate"
    assert claim["refdes"] is None
    assert set(claim["candidates"]) == {"L501", "L502", "L503", "L504"}
    assert claim["candidates"][0] == "L502"
    assert "closer photo" in claim["request"]


async def test_marking_and_registration_confirm_then_a_move_removes_it(bench: Bench) -> None:
    async with Client(bench.server) as client:
        photo, registration = await bench.ready(client)
        x, y = to_photo("U7301", dx_mm=2.0)
        claim = await identify(client, photo_id=photo, marking="U7301", registration_id=registration, x_px=x, y_px=y)
        before = (await client.call_tool("board_identity", {})).structured_content
        await bench.services.scene.mark_changed()
        after = (await client.call_tool("board_identity", {})).structured_content
        stale_photo = await client.call_tool("board_identify", {"photo_id": photo, "marking": "U7301"})

    assert claim["state"] == "confirmed"
    assert claim["refdes"] == "U7301"
    assert claim["basis"] == "marking"
    assert before is not None
    assert before["confirmed"] == ["U7301"]
    assert after is not None
    assert after["confirmed"] == []
    assert after["claims"][0]["state"] == "candidate"
    assert after["claims"][0]["candidates"][0] == "U7301"
    assert stale_photo.is_error


async def test_opposite_side_target_is_not_confirmed(bench: Bench) -> None:
    async with Client(bench.server) as client:
        photo, registration = await bench.ready(client)
        x, y = to_photo("U7303")
        claim = await identify(client, photo_id=photo, marking="U7303", registration_id=registration, x_px=x, y_px=y)

    assert claim["state"] == "visible_marking"
    assert claim["candidates"] == ["U7303"]
    assert "bottom side" in claim["reason"]
    assert "isolate the power" in claim["request"]


async def test_marking_without_registration_is_only_visible_marking(bench: Bench) -> None:
    async with Client(bench.server) as client:
        photo, _ = await bench.ready(client)
        exact = await identify(client, photo_id=photo, marking="U7301")
        prefix = await identify(client, photo_id=photo, marking="L50")

    assert exact["state"] == "visible_marking"
    assert exact["candidates"] == ["U7301"]
    assert "board_register_photo" in exact["request"]
    assert prefix["state"] == "visible_marking"
    assert prefix["candidates"] == ["L501", "L502", "L503", "L504"]


async def test_unique_landmark_needs_a_visual_input(bench: Bench) -> None:
    """QA B-E6: the agent's pixel and the boardview data alone are no visual input: a landmark stays a candidate
    until the user confirms it (or a marking is read)."""
    async with Client(bench.server) as client:
        photo, registration = await bench.ready(client)
        x, y = to_photo("J4")
        alone = await identify(client, photo_id=photo, registration_id=registration, x_px=x, y_px=y)
        confirmed = await identify(
            client, photo_id=photo, registration_id=registration, x_px=x, y_px=y, user_confirmed=True
        )

    assert alone["state"] == "candidate"
    assert alone["refdes"] is None
    assert alone["candidates"] == ["J4"]
    assert "no visual input" in alone["reason"]
    assert "user_confirmed" in alone["request"]
    assert confirmed["state"] == "confirmed"
    assert confirmed["refdes"] == "J4"
    assert confirmed["basis"] == "landmark"
    assert confirmed["user_confirmed"] is True


async def test_four_pair_registration_does_not_confirm(bench: Bench) -> None:
    async with Client(bench.server) as client:
        await client.call_tool("board_open", {"path": str(bench.board_file)})
        photo = photo_id(await client.call_tool("phone_snapshot", {}))
        registered = await client.call_tool(
            "board_register_photo", {"side": "top", "pairs": pairs(PAIR_PARTS[:4]), **SIZE}
        )
        registration = registered.structured_content["registration_id"]
        x, y = to_photo("U7301")
        claim = await identify(client, photo_id=photo, marking="U7301", registration_id=registration, x_px=x, y_px=y)

    assert claim["state"] == "visible_marking"
    assert "only 4 pairs" in claim["reason"]


async def test_locate_and_match_marking_never_confirm(bench: Bench) -> None:
    async with Client(bench.server) as client:
        _, registration = await bench.ready(client)
        located = await client.call_tool("board_locate_in_photo", {"registration_id": registration, "refdes": ["L501"]})
        matched = await client.call_tool("board_match_marking", {"marking": "U7301"})

    assert located.structured_content is not None
    assert located.structured_content["identity"] == "candidate"
    assert matched.structured_content is not None
    assert matched.structured_content["identity"] == "visible_marking"
