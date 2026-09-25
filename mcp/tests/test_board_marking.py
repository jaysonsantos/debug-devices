"""board_match_marking, the prefix fallback of board_find_part, and the evidence rules.

Fixture: mcp/tests/fixtures/boardview/markings.json. It is synthetic: every name in it is invented.
"""

from pathlib import Path

import pytest
from mcp import Client

from debug_devices_mcp.board.dump import BoardDump, Side
from debug_devices_mcp.board.marking import Resolution, find_marking_parts, normalize, unconfuse
from debug_devices_mcp.board.model import Board
from debug_devices_mcp.config import Settings
from debug_devices_mcp.instructions import EVIDENCE_RULES, server_instructions

from .test_board import fixture
from .test_board_tools import server_with, text

# The fake photo: 10 px per mm, y down, origin 100 px left of and 900 px below the board origin.
PX_PER_MM = 10.0
ORIGIN_PX = (100.0, 900.0)


def board() -> Board:
    return Board(BoardDump.model_validate_json(fixture("markings.json")), "sha")


def to_photo(x_mm: float, y_mm: float) -> tuple[float, float]:
    return ORIGIN_PX[0] + PX_PER_MM * x_mm, ORIGIN_PX[1] - PX_PER_MM * y_mm


def names(marking: str, side: Side | None = None) -> tuple[Resolution, list[str]]:
    resolution, parts = find_marking_parts(board(), marking, side)
    return resolution, [part.name for part in parts]


def test_normalize_and_confusions() -> None:
    assert normalize(" u-73 0_1 ") == "u7301"
    assert unconfuse("RIO") == unconfuse("R10")
    assert unconfuse("QI2") == unconfuse("Q12")
    assert unconfuse("SB8") == unconfuse("5b8")


@pytest.mark.parametrize(
    ("marking", "side", "resolution", "expected"),
    [
        ("Q12", None, Resolution.EXACT, ["Q12"]),
        ("q-1 2", None, Resolution.EXACT, ["Q12"]),
        ("U730", None, Resolution.PREFIX_CANDIDATES, ["U7301", "U7302", "U7303"]),
        ("U730", Side.TOP, Resolution.PREFIX_CANDIDATES, ["U7301", "U7302"]),
        ("8850", None, Resolution.CONTAINS_CANDIDATES, ["C8850"]),
        ("RIO", None, Resolution.CONFUSION_CANDIDATES, ["R10"]),
        ("QI2", None, Resolution.CONFUSION_CANDIDATES, ["Q12"]),
        ("X999", None, Resolution.NONE, []),
        ("", None, Resolution.NONE, []),
    ],
)
def test_resolutions(marking: str, side: Side | None, resolution: Resolution, expected: list[str]) -> None:
    assert names(marking, side) == (resolution, expected)


@pytest.fixture
def marking_file(tmp_path: Path) -> Path:
    path = tmp_path / "markings.cad"
    path.write_text("the fake obv-dump does not read this")
    return path


async def test_match_marking_tool_without_position(settings: Settings, marking_file: Path) -> None:
    server, _ = server_with(settings, fixture("markings.json"))
    async with Client(server) as client:
        await client.call_tool("board_open", {"path": str(marking_file)})
        prefix = await client.call_tool("board_match_marking", {"marking": "U730", "side": "top"})
        exact = await client.call_tool("board_match_marking", {"marking": "q12"})
        none = await client.call_tool("board_match_marking", {"marking": "X999"})
        no_registration = await client.call_tool("board_match_marking", {"marking": "U730", "x_px": 1, "y_px": 2})
        half_point = await client.call_tool("board_match_marking", {"marking": "U730", "x_px": 1})

    result = prefix.structured_content
    assert result is not None
    assert result["visible_marking"] == "U730"
    assert result["resolution"] == "prefix_candidates"
    assert [item["refdes"] for item in result["candidates"]] == ["U7301", "U7302"]
    assert result["best_candidate"] is None
    first = result["candidates"][0]
    assert first["mfgcode"] == "SYN-PMIC-A"
    assert first["pin_count"] == 8
    assert first["nets"] == ["GND", "PP_SYN_1V0", "SYN_EN_A", "SYN_FB_A"]
    assert first["photo_position"] is None
    assert result["message"].startswith('Visible marking "U730" has no exact boardview part. Candidates: U7301, U7302.')
    assert "board_register_photo" in result["message"]
    assert exact.structured_content is not None
    assert exact.structured_content["resolution"] == "exact"
    assert exact.structured_content["best_candidate"] == "Q12"
    assert exact.structured_content["visible_marking"] == "q12"
    assert none.structured_content is not None
    assert none.structured_content["resolution"] == "none"
    assert none.structured_content["candidates"] == []
    assert no_registration.is_error
    assert "registration_id" in text(no_registration)
    assert half_point.is_error


async def test_match_marking_ranks_by_photo_position(settings: Settings, marking_file: Path) -> None:
    parts = board().parts
    pairs = []
    for name in ("J4", "Q12", "TP9", "C8850", "R10"):
        x, y = to_photo(parts[name].center.x, parts[name].center.y)
        pairs.append({"refdes": name, "x_px": x, "y_px": y})
    near_u7301 = to_photo(parts["U7301"].center.x + 1.0, parts["U7301"].center.y)
    halfway = to_photo(
        (parts["U7301"].center.x + parts["U7302"].center.x) / 2, (parts["U7301"].center.y + parts["U7302"].center.y) / 2
    )
    server, _ = server_with(settings, fixture("markings.json"))
    async with Client(server) as client:
        await client.call_tool("board_open", {"path": str(marking_file)})
        registered = await client.call_tool(
            "board_register_photo", {"side": "top", "photo_width_px": 1200, "photo_height_px": 1000, "pairs": pairs}
        )
        assert registered.structured_content is not None, registered.content
        registration_id = registered.structured_content["registration_id"]
        args = {"marking": "U730", "side": "top", "registration_id": registration_id}
        near = await client.call_tool("board_match_marking", {**args, "x_px": near_u7301[0], "y_px": near_u7301[1]})
        middle = await client.call_tool("board_match_marking", {**args, "x_px": halfway[0], "y_px": halfway[1]})

    result = near.structured_content
    assert result is not None
    assert [item["refdes"] for item in result["candidates"]] == ["U7301", "U7302"]
    assert result["best_candidate"] == "U7301"
    assert result["candidates"][0]["distance_px"] == pytest.approx(PX_PER_MM, abs=0.5)
    assert result["candidates"][0]["photo_position"]["x_px"] == pytest.approx(to_photo(parts["U7301"].center.x, 0)[0])
    assert "the best candidate is U7301" in result["message"]
    assert middle.structured_content is not None
    assert middle.structured_content["best_candidate"] is None
    assert "does not tell them apart" in middle.structured_content["message"]


async def test_find_part_falls_back_to_prefix(settings: Settings, marking_file: Path) -> None:
    server, _ = server_with(settings, fixture("markings.json"))
    async with Client(server) as client:
        await client.call_tool("board_open", {"path": str(marking_file)})
        prefix = await client.call_tool("board_find_part", {"query": "U730"})
        exact = await client.call_tool("board_find_part", {"query": "U7301"})
        none = await client.call_tool("board_find_part", {"query": "ZZZ"})

    assert prefix.structured_content is not None
    assert prefix.structured_content["match"] == "prefix"
    assert [part["name"] for part in prefix.structured_content["parts"]] == ["U7301", "U7302", "U7303"]
    assert "board_match_marking" in prefix.structured_content["note"]
    assert exact.structured_content is not None
    assert exact.structured_content["match"] == "name_or_mfgcode"
    assert exact.structured_content["note"] is None
    assert none.structured_content is not None
    assert none.structured_content["match"] == "none"
    assert none.structured_content["parts"] == []


async def test_evidence_rules_in_instructions_and_descriptions(settings: Settings, tmp_path: Path) -> None:
    text_with_rules = server_instructions(tmp_path / "missing.md", "Tool guide.")
    assert EVIDENCE_RULES in text_with_rules
    for phrase in (
        "fresh phone_snapshot",
        "only multimeter_read",
        "supporting evidence",
        "board_match_marking",
        "user's cockpit",
        "call phone_zoom, then take a fresh",
        "Zoom out when you need wider context",
        "not proof of what appears in a physical photo",
        "current photo cannot confirm it",
        "isolate the power",
        "assume that distance gives detail and zoom does not",
        "you may turn on phone_in_sensor_zoom and use a zoom of 2x-4x",
        "It is not optical zoom. Still take a fresh phone_snapshot after the change.",
        "focus on it with phone_focus",
        "you may call phone_highlight",
        "never reuse boxes or positions from an older photo",
    ):
        assert phrase in EVIDENCE_RULES

    server, _ = server_with(settings, fixture("markings.json"))
    async with Client(server) as client:
        assert client.instructions is not None
        assert EVIDENCE_RULES in client.instructions
        tools = {tool.name: tool.description or "" for tool in (await client.list_tools()).tools}

    assert "every question about what is visible on the device" in tools["phone_snapshot"]
    assert "only to check the multimeter framing" in tools["webcam_snapshot"]
    assert "not to read the meter" in tools["webcam_snapshot"]
    assert "not a photo" in tools["board_render"]
    assert "never proof of what is physically visible" in tools["board_render"]
    assert "the only tool for meter values" in tools["multimeter_read"]
    assert "never replace the" in tools["board_match_marking"]
