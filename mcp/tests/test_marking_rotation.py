"""Rotated markings (board/rotation.py), SMD value codes (board/value_code.py), and board_match_marking with both.

Run with `uv run pytest mcp/tests/test_marking_rotation.py`. The board is the synthetic markings.json fixture.
"""

from pathlib import Path

import pytest
from mcp import Client

from debug_devices_mcp.board.dump import BoardDump
from debug_devices_mcp.board.marking import Resolution
from debug_devices_mcp.board.marking_readings import lookup_marking
from debug_devices_mcp.board.model import Board
from debug_devices_mcp.board.rotation import (
    ONE_WAY,
    ROTATION_180,
    SELF_SYMMETRIC,
    SYMMETRIC_PAIRS,
    Reading,
    read_rotated_180,
)
from debug_devices_mcp.board.value_code import E96_VALUES, ValueCodeKind, decode_value_marking
from debug_devices_mcp.config import Settings

from .test_board import fixture
from .test_board_tools import server_with

E96_SERIES_LENGTH = 96


def board() -> Board:
    return Board(BoardDump.model_validate_json(fixture("markings.json")), "sha")


# region: rotation table


def test_pairs_turn_into_each_other() -> None:
    for first, second in SYMMETRIC_PAIRS:
        assert ROTATION_180[first] == second
        assert ROTATION_180[second] == first
    for char in SELF_SYMMETRIC:
        assert ROTATION_180[char] == char
    for seen, upright in ONE_WAY:
        assert ROTATION_180[seen] == upright


def test_every_character_has_one_role() -> None:
    paired = [char for pair in SYMMETRIC_PAIRS for char in pair]
    one_way = [seen for seen, _ in ONE_WAY]
    roles = list(SELF_SYMMETRIC) + paired + one_way
    assert len(roles) == len(set(roles))


@pytest.mark.parametrize(
    ("seen", "upright"),
    [
        # From the bench: a resistor marked "100", read as "00T" in a turned photo.
        ("00T", "100"),
        # U7301 upside down: 1 0 E L n.
        ("10ELn", "u7301"),
        ("69", "69"),
        ("6", "9"),
        ("SOS", "SOS"),
        ("", ""),
    ],
)
def test_read_rotated_180(seen: str, upright: str) -> None:
    reading = read_rotated_180(seen)
    assert reading.text == upright
    assert reading.unmapped == []


def test_characters_without_an_upright_form() -> None:
    reading = read_rotated_180("R19")
    assert reading.text is None
    assert reading.unmapped == ["R"]


# endregion: rotation table

# region: value codes


@pytest.mark.parametrize(
    ("code", "kind", "value_ohm", "shown"),
    [
        ("100", ValueCodeKind.EIA_3_DIGIT, 10.0, "10 Ω"),
        ("472", ValueCodeKind.EIA_3_DIGIT, 4700.0, "4.7 kΩ"),
        ("105", ValueCodeKind.EIA_3_DIGIT, 1_000_000.0, "1 MΩ"),
        ("000", ValueCodeKind.EIA_3_DIGIT, 0.0, "0 Ω"),
        ("0", ValueCodeKind.JUMPER, 0.0, "0 Ω"),
        ("1002", ValueCodeKind.EIA_4_DIGIT, 10_000.0, "10 kΩ"),
        ("4R7", ValueCodeKind.LETTER_DECIMAL, 4.7, "4.7 Ω"),
        ("1R0", ValueCodeKind.LETTER_DECIMAL, 1.0, "1 Ω"),
        ("R19", ValueCodeKind.LETTER_DECIMAL, 0.19, "0.19 Ω"),
        ("4k7", ValueCodeKind.LETTER_DECIMAL, 4700.0, "4.7 kΩ"),
        ("01C", ValueCodeKind.EIA_96, 10_000.0, "10 kΩ"),
        ("68X", ValueCodeKind.EIA_96, 49.9, "49.9 Ω"),
    ],
)
def test_decode_value_marking(code: str, kind: ValueCodeKind, value_ohm: float, shown: str) -> None:
    [result] = decode_value_marking(code)
    assert result.kind is kind
    assert result.value_ohm == pytest.approx(value_ohm)
    assert result.display == shown
    assert result.interpretation_only
    assert "Interpretation only" in result.note


def test_ambiguous_code_gives_every_reading() -> None:
    results = decode_value_marking("47R")
    assert [(item.kind, item.display) for item in results] == [
        (ValueCodeKind.EIA_96, "3.01 Ω"),
        (ValueCodeKind.LETTER_DECIMAL, "47 Ω"),
    ]


def test_same_value_twice_is_kept_once() -> None:
    assert [item.kind for item in decode_value_marking("01R")] == [ValueCodeKind.EIA_96]


@pytest.mark.parametrize("text", ["U7301", "R", "ABC", "109", "99A", ""])
def test_not_a_value_code(text: str) -> None:
    assert decode_value_marking(text) == []


def test_e96_series() -> None:
    assert len(E96_VALUES) == E96_SERIES_LENGTH
    assert list(E96_VALUES) == sorted(E96_VALUES)


# endregion: value codes

# region: board_match_marking


def test_lookup_uses_the_rotated_reading_only_when_it_is_better() -> None:
    turned = lookup_marking(board(), "10ELn", None, try_rotations=True)
    assert turned.reading is Reading.ROTATED_180
    assert turned.resolution is Resolution.EXACT
    assert [part.name for part in turned.parts] == ["U7301"]
    assert turned.rotated_text == "u7301"

    upright = lookup_marking(board(), "U730", None, try_rotations=True)
    assert upright.reading is Reading.AS_SEEN
    assert upright.resolution is Resolution.PREFIX_CANDIDATES

    off = lookup_marking(board(), "10ELn", None, try_rotations=False)
    assert off.reading is Reading.AS_SEEN
    assert off.resolution is Resolution.NONE
    assert off.rotated_text is None


def test_lookup_reads_value_codes_of_both_readings() -> None:
    lookup = lookup_marking(board(), "00T", None, try_rotations=True)
    assert lookup.resolution is Resolution.NONE
    assert lookup.rotated_text == "100"
    assert [item.display for item in lookup.value_interpretations] == ["10 Ω"]


@pytest.fixture
def marking_file(tmp_path: Path) -> Path:
    path = tmp_path / "markings.cad"
    path.write_text("the fake obv-dump does not read this")
    return path


async def test_match_marking_tool_reads_upside_down(settings: Settings, marking_file: Path) -> None:
    server, _ = server_with(settings, fixture("markings.json"))
    async with Client(server) as client:
        await client.call_tool("board_open", {"path": str(marking_file)})
        turned = await client.call_tool("board_match_marking", {"marking": "10ELn"})
        off = await client.call_tool("board_match_marking", {"marking": "10ELn", "try_rotations": False})
        value = await client.call_tool("board_match_marking", {"marking": "00T"})

    result = turned.structured_content
    assert result is not None
    assert result["visible_marking"] == "10ELn"
    assert result["reading"] == "rotated_180"
    assert result["rotated_reading"] == "u7301"
    assert result["resolution"] == "exact"
    assert result["best_candidate"] == "U7301"
    assert result["message"].startswith('Read upside down (turned 180 degrees), the visible marking "10ELn" is')
    assert off.structured_content is not None
    assert off.structured_content["resolution"] == "none"
    assert off.structured_content["reading"] == "as_seen"
    assert off.structured_content["rotated_reading"] is None
    assert value.structured_content is not None
    [code] = value.structured_content["value_interpretations"]
    assert (code["code"], code["display"], code["interpretation_only"]) == ("100", "10 Ω", True)
    assert "interpretation only" in value.structured_content["message"]


# endregion: board_match_marking
