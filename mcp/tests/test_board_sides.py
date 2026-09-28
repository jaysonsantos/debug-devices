"""Side labels of boardview files (bench feedback 2, item 8): through-hole parts and mounting holes are on both sides,
and a file with mixed top/bottom labels gives a warning instead of a refusal.

Fixtures (synthetic, invented names): sides.json (mixed labels) and identity.json (clean labels).
"""

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from debug_devices_mcp.board.dump import BoardDump, Side
from debug_devices_mcp.board.loader import LoaderOptions
from debug_devices_mcp.board.model import Board, SideLabels
from debug_devices_mcp.board.session_store import BoardSessionStore
from debug_devices_mcp.board.tools import BoardSession
from debug_devices_mcp.config import Settings
from debug_devices_mcp.process import CommandResult
from debug_devices_mcp.server import build_server

from .conftest import FakeRunner
from .test_board import fixture
from .test_board_identity import photo_id
from .test_server import FakePhone, make_services, no_vision

PX_PER_MM = 10.0
ORIGIN_PX = (100.0, 900.0)
PAIRS = ("MH1", "H2", "J5", "U801", "U802", "U803")
SIZE = {"photo_width_px": 1200, "photo_height_px": 1000}


def board(name: str = "sides.json") -> Board:
    return Board(BoardDump.model_validate_json(fixture(name)), "sha")


def to_photo(name: str) -> tuple[float, float]:
    center = board().parts[name].center
    return ORIGIN_PX[0] + PX_PER_MM * center.x, ORIGIN_PX[1] - PX_PER_MM * center.y


def test_mounting_holes_and_through_hole_parts_are_on_both_sides() -> None:
    parts = board().parts
    for name in ("MH1", "H2", "J5"):
        assert parts[name].side is Side.BOTH
        assert parts[name].labeled_side is Side.TOP
    assert parts["U801"].labeled_side is None
    assert all(pin.side is Side.BOTH for pin in board().pins_by_part["J5"])


def test_mixed_side_labels_are_detected() -> None:
    mixed = board().side_check
    assert mixed.mixed is True
    assert mixed.opposite_pairs >= 10
    assert mixed.warning is not None
    clean = board("identity.json").side_check
    assert clean.mixed is False
    assert clean.warning is None


class Bench:
    def __init__(self, settings: Settings, tmp_path: Path) -> None:
        self.services = make_services(settings, FakePhone(), no_vision())
        runner = FakeRunner(lambda _: CommandResult(returncode=0, stdout=fixture("sides.json"), stderr=b""))
        self.services.board = BoardSession.create(runner, LoaderOptions(dump_bin="obv-dump", timeout=timedelta(5)))
        self.server = build_server(self.services)
        self.board_file = tmp_path / "sides.cad"
        self.board_file.write_text("x")


@pytest.fixture
def bench(settings: Settings, tmp_path: Path) -> Bench:
    return Bench(settings, tmp_path)


async def ready(client: Client, bench: Bench) -> tuple[dict[str, Any], str, str]:
    opened = await client.call_tool("board_open", {"path": str(bench.board_file)})
    photo = photo_id(await client.call_tool("phone_snapshot", {}))
    pairs = [{"refdes": name, "x_px": to_photo(name)[0], "y_px": to_photo(name)[1]} for name in PAIRS]
    registered = await client.call_tool("board_register_photo", {"side": "top", "pairs": pairs, **SIZE})
    assert opened.structured_content is not None
    assert registered.structured_content is not None, registered.content
    return opened.structured_content, photo, registered.structured_content["registration_id"]


async def test_tools_warn_instead_of_ruling_out(bench: Bench) -> None:
    async with Client(bench.server) as client:
        summary, photo, registration = await ready(client, bench)
        x, y = to_photo("C902")
        near = await client.call_tool("board_parts_at_photo", {"registration_id": registration, "x_px": x, "y_px": y})
        ux, uy = to_photo("U804")
        claim = await client.call_tool(
            "board_identify",
            {"photo_id": photo, "marking": "U804", "registration_id": registration, "x_px": ux, "y_px": uy},
        )
        located = await client.call_tool("board_locate_in_photo", {"registration_id": registration, "refdes": ["U804"]})

    assert summary["side_warning"] is not None
    assert summary["relabeled_both_sides"] == 3
    assert near.structured_content is not None
    assert near.structured_content["parts"][0]["refdes"] == "C902"
    assert near.structured_content["parts"][0]["side"] == "bottom"  # listed although the registration is top
    assert near.structured_content["side_warning"] is not None
    # The marked part is labelled bottom, but the labels are mixed: the photo decides, with a note.
    assert claim.structured_content is not None
    assert claim.structured_content["state"] == "confirmed"
    assert "labels are mixed" in claim.structured_content["reason"]
    assert located.structured_content is not None
    assert any("look mixed" in note for note in located.structured_content["notes"])


def test_side_label_choice_overrides_the_heuristic() -> None:
    clean = board("identity.json")
    clean.apply_side_labels(SideLabels.MIXED)
    assert clean.mixed_sides is True
    assert "the user said" in (clean.side_check.warning or "")
    mixed = board()
    mixed.apply_side_labels(SideLabels.TRUST)
    assert mixed.mixed_sides is False
    assert mixed.side_check.warning is None
    mixed.apply_side_labels(SideLabels.AUTO)
    assert mixed.mixed_sides is True


async def test_side_labels_mixed_keeps_over_a_restart(settings: Settings, tmp_path: Path) -> None:
    store_path = tmp_path / "board-session.json"
    board_file = tmp_path / "identity.cad"
    board_file.write_text("x")

    def process() -> Any:
        services = make_services(settings, FakePhone(), no_vision())
        runner = FakeRunner(lambda _: CommandResult(returncode=0, stdout=fixture("identity.json"), stderr=b""))
        services.board = BoardSession.create(runner, LoaderOptions(dump_bin="obv-dump", timeout=timedelta(5)))
        services.board.with_store(BoardSessionStore(store_path))
        return build_server(services), services

    first, _ = process()
    async with Client(first) as client:
        opened = await client.call_tool("board_open", {"path": str(board_file), "side_labels": "mixed"})
    assert opened.structured_content is not None
    assert "the user said" in opened.structured_content["side_warning"]

    second, services = process()
    async with Client(second) as client:
        await client.call_tool("board_find_part", {"query": "J4"})
    assert services.board.current().mixed_sides is True
