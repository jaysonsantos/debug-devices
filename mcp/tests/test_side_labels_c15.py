"""QA contract gap C15: every side check uses Board.side_ok, so a file with mixed side labels does not rule a part
out by its label (phone_point_to, board_match_marking, board_locate_in_photo, board_parts_near).

Run with `uv run pytest mcp/tests/test_side_labels_c15.py`. Fixtures (synthetic, invented names): sides.json has
mixed labels (U804 and C902 are labelled bottom), markings.json has clean labels (U7303 is on the bottom).
"""

from datetime import timedelta
from pathlib import Path

import numpy as np
from mcp import Client

from debug_devices_mcp.board.dump import BoardDump, Side
from debug_devices_mcp.board.loader import LoaderOptions
from debug_devices_mcp.board.marking import Resolution, find_marking_parts
from debug_devices_mcp.board.model import Board
from debug_devices_mcp.board.tools import BoardSession
from debug_devices_mcp.config import Settings
from debug_devices_mcp.images import SnapshotOrientation
from debug_devices_mcp.pointer import MIXED_LABEL_NOTE, OTHER_SIDE_MESSAGE, ImageFrame, plan
from debug_devices_mcp.process import CommandResult
from debug_devices_mcp.server import Services, build_server

from .conftest import FakeRunner
from .test_board import fixture
from .test_board_sides import PAIRS, SIZE, to_photo
from .test_highlight import OverlayPhone
from .test_pointing import PHOTO_HEIGHT, PHOTO_WIDTH, blank_photo
from .test_server import make_services, no_vision

PX_PER_MM = 10.0
ORIGIN_PX = (100.0, 900.0)


def board(name: str) -> Board:
    return Board(BoardDump.model_validate_json(fixture(name)), "sha")


def services_for(settings: Settings, name: str) -> Services:
    phone = OverlayPhone()
    phone.snapshot = blank_photo()
    services = make_services(settings, phone, no_vision())
    runner = FakeRunner(lambda _: CommandResult(returncode=0, stdout=fixture(name), stderr=b""))
    services.board = BoardSession.create(runner, LoaderOptions(dump_bin="obv-dump", timeout=timedelta(seconds=5)))
    return services


async def open_and_register(client: Client, board_file: Path) -> str:
    await client.call_tool("board_open", {"path": str(board_file)})
    await client.call_tool("phone_snapshot", {})
    pairs = [{"refdes": name, "x_px": to_photo(name)[0], "y_px": to_photo(name)[1]} for name in PAIRS]
    registered = await client.call_tool("board_register_photo", {"side": "top", "pairs": pairs, **SIZE})
    assert registered.structured_content is not None, registered.content
    return registered.structured_content["registration_id"]


def board_to_photo() -> np.ndarray:
    """Board mm (y up) to the photo pixels of test_board_sides.to_photo (10 px per mm, y down)."""
    return np.array([[PX_PER_MM, 0, ORIGIN_PX[0]], [0, -PX_PER_MM, ORIGIN_PX[1]], [0, 0, 1]])


def test_marking_search_keeps_mixed_labels_and_rules_out_clean_ones() -> None:
    assert find_marking_parts(board("sides.json"), "U804", Side.TOP) == (
        Resolution.EXACT,
        [board("sides.json").parts["U804"]],
    )
    # A clean file still rules a part out by its label.
    assert find_marking_parts(board("markings.json"), "U7303", Side.TOP) == (Resolution.NONE, [])


def test_plan_uses_the_side_rule_of_the_board() -> None:
    mixed = board("sides.json")
    part = mixed.parts["U804"]
    frame = ImageFrame((PHOTO_WIDTH, PHOTO_HEIGHT), SnapshotOrientation())
    by_label = plan([part], Side.TOP, board_to_photo(), frame)
    by_board = plan([part], Side.TOP, board_to_photo(), frame, mixed.side_ok)

    [ruled_out] = by_label.targets
    assert ruled_out.message == OTHER_SIDE_MESSAGE
    assert by_label.boxes == []
    [target] = by_board.targets
    assert target.in_view is not None
    assert target.message.endswith(MIXED_LABEL_NOTE.format(side=Side.BOTTOM))
    assert len(by_board.boxes) + len(by_board.arrows) == 1


async def test_tools_do_not_rule_out_a_mixed_label(settings: Settings, tmp_path: Path) -> None:
    board_file = tmp_path / "sides.cad"
    board_file.write_text("the fake obv-dump does not read this")
    services = services_for(settings, "sides.json")
    async with Client(build_server(services)) as client:
        registration = await open_and_register(client, board_file)
        pointed = await client.call_tool("phone_point_to", {"refdes": "U804", "registration_id": registration})
        matched = await client.call_tool("board_match_marking", {"marking": "U804", "side": "top"})
        located = await client.call_tool("board_locate_in_photo", {"registration_id": registration, "refdes": ["U804"]})
        near = await client.call_tool("board_parts_near", {"refdes": "C901", "radius_mm": 50, "side": "top"})

    assert not pointed.is_error, pointed.content
    assert pointed.structured_content is not None
    [target] = pointed.structured_content["targets"]
    assert target["in_view"] is not None
    assert "labels are mixed" in target["message"]
    assert matched.structured_content is not None
    assert matched.structured_content["resolution"] == "exact"
    assert matched.structured_content["side_warning"] is not None
    assert located.structured_content is not None
    assert located.structured_content["parts"][0]["on_registered_side"] is True
    assert near.structured_content is not None
    assert "C902" in [item["part"]["name"] for item in near.structured_content["parts"]]
    assert near.structured_content["side_warning"] is not None


async def test_clean_labels_still_rule_out_the_other_side(settings: Settings, tmp_path: Path) -> None:
    board_file = tmp_path / "markings.cad"
    board_file.write_text("the fake obv-dump does not read this")
    services = services_for(settings, "markings.json")
    async with Client(build_server(services)) as client:
        await client.call_tool("board_open", {"path": str(board_file)})
        matched = await client.call_tool("board_match_marking", {"marking": "U7303", "side": "top"})
        near = await client.call_tool("board_parts_near", {"refdes": "U7301", "radius_mm": 500, "side": "top"})

    assert matched.structured_content is not None
    assert matched.structured_content["resolution"] == "none"
    assert matched.structured_content["side_warning"] is None
    assert near.structured_content is not None
    assert "U7303" not in [item["part"]["name"] for item in near.structured_content["parts"]]
    assert near.structured_content["side_warning"] is None
