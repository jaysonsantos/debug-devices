"""Tests for obv-dump against docs/boardview-json.md.

Run with boardview/tests/run.sh. obv-dump comes from the dev shell, or from OBV_DUMP_BIN.
The local-only test for a real board file runs when BOARDVIEW_TARGET is set.
"""

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

# region: constants

ENV_BIN = "OBV_DUMP_BIN"
ENV_TARGET = "BOARDVIEW_TARGET"
BIN_NAME = "obv-dump"
FIXTURES = Path(__file__).parent / "fixtures"
TIMEOUT_SECONDS = 60
SCHEMA_VERSION = 1
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
FZ_KEY_WORDS = 44
# rotation.cad: R1 has ROTATION 90, R2 has ROTATION 0.
R1_ROTATION_DEG = 90.0
R2_ROTATION_DEG = 0.0

BOARD_KEYS = {"schema_version", "source", "units", "outline", "outline_segments", "parts", "pins", "nails"}
PART_KEYS = {"name", "side", "mounting", "p1", "p2", "rotation_deg", "mfgcode", "first_pin", "pin_count"}
PIN_KEYS = {"part", "number", "name", "net", "x", "y", "side", "radius", "probe"}
NAIL_KEYS = {"net", "x", "y", "side", "probe"}
ERROR_KEYS = {"schema_version", "error", "message", "format"}
SIDES = {"top", "bottom", "both"}
MOUNTINGS = {"smd", "through_hole"}

# endregion: constants


@dataclass(frozen=True)
class Expected:
    fixture: str
    format: str
    parts: int
    pins: int
    nails: int
    outline: int
    outline_segments: int


# Counts from obv-dump 0.1.0 with OpenBoardView 10.0.0.
EXPECTED = [
    Expected("example.brd", "brd2", parts=245, pins=1130, nails=19, outline=73, outline_segments=0),
    Expected("example.bvr", "bvr3", parts=272, pins=1149, nails=0, outline=73, outline_segments=0),
    Expected("rotation.cad", "gencad", parts=2, pins=4, nails=0, outline=0, outline_segments=4),
]


def obv_dump_bin() -> str:
    path = os.environ.get(ENV_BIN) or shutil.which(BIN_NAME)
    if not path:
        pytest.fail(f"{BIN_NAME} is not on PATH. Run the tests in the dev shell or set {ENV_BIN}.")
    return path


def run(*args: str | Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [obv_dump_bin(), *map(str, args)],
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        check=False,
    )


def dump(path: Path) -> dict[str, Any]:
    result = run(path)
    assert result.returncode == EXIT_OK, result.stdout + result.stderr
    return json.loads(result.stdout)


def error(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    assert result.returncode == EXIT_FAILED, result.stdout + result.stderr
    body = json.loads(result.stdout)
    assert set(body) == ERROR_KEYS
    assert body["schema_version"] == SCHEMA_VERSION
    return body


def check_contract(board: dict[str, Any], path: Path, format_name: str) -> None:
    """Checks the shape rules of docs/boardview-json.md that hold for every board."""
    assert set(board) == BOARD_KEYS
    assert board["schema_version"] == SCHEMA_VERSION
    assert board["units"] == "mil"
    assert board["source"]["path"] == str(path.resolve())
    assert board["source"]["format"] == format_name

    names = [part["name"] for part in board["parts"]]
    assert len(names) == len(set(names)), "part names are unique"
    for part in board["parts"]:
        assert set(part) == PART_KEYS
        assert part["side"] in SIDES
        assert part["mounting"] in MOUNTINGS
        assert (part["p1"] is None) == (part["p2"] is None)
        if part["p1"] is not None:
            assert (part["p1"], part["p2"]) != ({"x": 0, "y": 0}, {"x": 0, "y": 0})
        if part["pin_count"]:
            first = board["pins"][part["first_pin"]]
            assert first["part"] == part["name"]
        else:
            assert part["first_pin"] is None

    known = set(names)
    for pin in board["pins"]:
        assert set(pin) == PIN_KEYS
        assert pin["part"] in known
        assert pin["side"] in SIDES
        assert isinstance(pin["x"], int)
        assert isinstance(pin["y"], int)
    for nail in board["nails"]:
        assert set(nail) == NAIL_KEYS
        assert nail["side"] in SIDES
    for segment in board["outline_segments"]:
        assert set(segment) == {"a", "b"}


def test_version() -> None:
    result = run("--version")
    assert result.returncode == EXIT_OK
    assert result.stdout.startswith(BIN_NAME)
    assert "OpenBoardView 10.0.0" in result.stdout


@pytest.mark.parametrize("expected", EXPECTED, ids=lambda e: e.fixture)
def test_fixture(expected: Expected) -> None:
    path = FIXTURES / expected.fixture
    board = dump(path)

    check_contract(board, path, expected.format)
    assert len(board["parts"]) == expected.parts
    assert len(board["pins"]) == expected.pins
    assert len(board["nails"]) == expected.nails
    assert len(board["outline"]) == expected.outline
    assert len(board["outline_segments"]) == expected.outline_segments
    assert sum(part["pin_count"] for part in board["parts"]) == expected.pins


def test_brd_has_boxes_and_no_rotation() -> None:
    board = dump(FIXTURES / "example.brd")
    assert all(part["p1"] is not None for part in board["parts"])
    assert all(part["rotation_deg"] is None for part in board["parts"])


def test_bvr_has_no_boxes() -> None:
    board = dump(FIXTURES / "example.bvr")
    assert all(part["p1"] is None for part in board["parts"])
    assert any(pin["net"] == "/SCL" for pin in board["pins"])


def test_repeated_part_names_get_a_suffix() -> None:
    # The KiCad example has seven mounting holes named "REF**".
    board = dump(FIXTURES / "example.bvr")
    names = {part["name"] for part in board["parts"]}
    assert {"REF**", "REF**#2", "REF**#7"} <= names
    assert "REF**#8" not in names


def test_gencad_rotation_and_sides() -> None:
    board = dump(FIXTURES / "rotation.cad")
    parts = {part["name"]: part for part in board["parts"]}
    assert parts["R1"]["rotation_deg"] == R1_ROTATION_DEG
    assert parts["R1"]["side"] == "top"
    assert parts["R2"]["rotation_deg"] == R2_ROTATION_DEG
    assert parts["R2"]["side"] == "bottom"

    pins = {(pin["part"], pin["number"]): pin for pin in board["pins"]}
    # R1: pin 1 at shape offset (-20, 0) turned 90 degrees around PLACE 200 250.
    assert (pins["R1", "1"]["x"], pins["R1", "1"]["y"]) == (200, 230)
    assert pins["R1", "1"]["net"] == "VCC"
    # R2 is on the bottom: X is mirrored.
    assert (pins["R2", "1"]["x"], pins["R2", "1"]["y"]) == (680, 250)
    assert pins["R2", "1"]["side"] == "bottom"


def test_unknown_format(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("not a board file\n")
    body = error(run(path))
    assert body["error"] == "unknown_format"
    assert body["format"] is None


def test_missing_file(tmp_path: Path) -> None:
    body = error(run(tmp_path / "missing.brd"))
    assert body["error"] == "io_error"


def test_fz_without_key(tmp_path: Path) -> None:
    path = tmp_path / "board.fz"
    path.write_bytes(bytes(range(256)))
    body = error(run(path))
    assert body["error"] == "key_required"
    assert body["format"] == "fz"


def test_fz_with_wrong_key_does_not_echo_key(tmp_path: Path) -> None:
    path = tmp_path / "board.fz"
    path.write_bytes(bytes(range(256)))
    key = ",".join(["0x0"] * FZ_KEY_WORDS)
    result = run(path, "--fz-key", key)
    body = error(result)
    assert body["error"] == "key_invalid"
    assert "0x" not in result.stdout


def test_bad_key_flag_is_usage_error(tmp_path: Path) -> None:
    result = run(tmp_path / "board.fz", "--fz-key", "0x1")
    assert result.returncode == EXIT_USAGE


@pytest.mark.skipif(not os.environ.get(ENV_TARGET), reason=f"{ENV_TARGET} is not set (local-only test)")
def test_target_file() -> None:
    path = Path(os.environ[ENV_TARGET])
    board = dump(path)
    check_contract(board, path, board["source"]["format"])
    assert board["parts"]
    assert board["pins"]
