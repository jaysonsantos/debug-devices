"""Board model, obv-dump loader, and renderer. Fixtures: mcp/tests/fixtures/boardview/ (open data only)."""

import io
from datetime import timedelta
from pathlib import Path

import pytest
from PIL import Image
from pydantic import SecretStr

from debug_devices_mcp.board.dump import BoardDump, Side
from debug_devices_mcp.board.loader import (
    BoardDumpFailed,
    BoardLoadError,
    BoardviewKeys,
    BoardviewLoader,
    LoaderOptions,
)
from debug_devices_mcp.board.model import Board
from debug_devices_mcp.board.render import RenderError, RenderOptions, render_board
from debug_devices_mcp.board.units import mil_to_mm, mm_to_mil
from debug_devices_mcp.process import CommandError, CommandResult

from .conftest import FakeRunner

FIXTURES = Path(__file__).parent / "fixtures" / "boardview"
KEY = "0123456789abcdef-secret"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def board(name: str = "tiny.json") -> Board:
    return Board(BoardDump.model_validate_json(fixture(name)), "sha")


def loader(runner: FakeRunner, keys: BoardviewKeys | None = None) -> BoardviewLoader:
    options = LoaderOptions(dump_bin="obv-dump", timeout=timedelta(seconds=5), keys=keys or BoardviewKeys())
    return BoardviewLoader(runner, options)


# region: model


def test_units_round_trip() -> None:
    assert mil_to_mm(1000) == pytest.approx(25.4)
    assert mm_to_mil(mil_to_mm(1234)) == pytest.approx(1234)


def test_part_box_from_file_and_from_pins() -> None:
    tiny = board()
    u1, r1 = tiny.parts["U1"], tiny.parts["R1"]

    assert not u1.box_from_pins
    assert (u1.box.min_x, u1.box.max_x) == (pytest.approx(mil_to_mm(980)), pytest.approx(mil_to_mm(1120)))
    assert (u1.center.x, u1.center.y) == (pytest.approx(mil_to_mm(1050)), pytest.approx(mil_to_mm(550)))
    assert u1.rotation_deg == 90.0
    # R1 has no box: the box is around its pins plus the pin radius (10 mil = 0.254 mm, below the 0.3 mm margin).
    assert r1.box_from_pins
    assert (r1.center.x, r1.center.y) == (pytest.approx(mil_to_mm(750)), pytest.approx(mil_to_mm(550)))
    assert r1.box.height == pytest.approx(0.6)
    assert r1.nets == ["EN", "PP3V3"]


def test_point_box_from_file_uses_the_pins() -> None:
    dump = BoardDump.model_validate_json(fixture("tiny.json"))
    dump.parts[0].p2 = dump.parts[0].p1  # U1: p1 == p2, as some GenCAD converters write it
    u1 = Board(dump, "sha").parts["U1"]
    assert u1.box_from_pins
    assert u1.box.width == pytest.approx(mil_to_mm(100) + 2 * 0.3)


def test_find_parts_by_name_glob_and_mfgcode() -> None:
    tiny = board()
    assert [part.name for part in tiny.find_parts("u1")] == ["U1"]
    assert [part.name for part in tiny.find_parts("U?")] == ["U1", "U2"]
    assert [part.name for part in tiny.find_parts("10k")] == ["R1"]
    assert [part.name for part in tiny.find_parts("TPS62130*")] == ["U1", "U2"]
    assert tiny.find_parts("Q9") == []


def test_nets_and_test_points() -> None:
    tiny = board()
    assert tiny.net_names("pp3v3") == ["PP3V3"]
    assert tiny.net_names("PP*") == ["PP1V8", "PP3V3"]
    # The unconnected pin of U2 is on no net.
    assert "" not in tiny.pins_by_net
    kinds = {(point.kind, point.name, point.net) for point in tiny.test_points}
    assert ("part", "TP1", "PP1V8") in kinds
    assert ("nail", "nail 1", "PP3V3") in kinds
    nearest = tiny.nearest_test_point(tiny.parts["U2"], "PP3V3")
    assert nearest is not None
    assert nearest.name == "nail 3"


def test_parts_near_with_side_filter() -> None:
    tiny = board()
    center = tiny.parts["U1"].center
    near = tiny.parts_near(center, 10.0, None, exclude="U1")
    assert [part.name for part in near][:2] == ["R1", "C1"]
    assert "C1" not in [part.name for part in tiny.parts_near(center, 10.0, Side.TOP, exclude="U1")]


def test_open_example_board() -> None:
    """Real obv-dump output for the open whitequark example (BRD2: boxes set, no pin numbers)."""
    example = board("example.brd.json")
    assert example.format == "brd2"
    assert len(example.parts) == 245
    assert sum(len(pins) for pins in example.pins_by_part.values()) == 1130
    assert not any(part.box_from_pins for part in example.parts.values())
    u1 = example.part("u1")
    assert u1 is not None
    # The file has no pin numbers, so the model numbers the pins in file order.
    assert [pin.number for pin in example.pins_by_part[u1.name]][:3] == ["1", "2", "3"]
    # The bounds come from the outline and the pins, not from part boxes that reach past the board edge.
    outline = example.outline[0]
    assert example.bounds.min_y == pytest.approx(min(point.y for point in outline))


# endregion: model

# region: loader


async def test_loader_runs_obv_dump_and_caches(tmp_path: Path) -> None:
    source = tmp_path / "tiny.cad"
    source.write_text("not read by the fake runner")
    runner = FakeRunner(lambda _: CommandResult(returncode=0, stdout=fixture("tiny.json"), stderr=b""))
    keys = BoardviewKeys(fz=SecretStr(KEY))

    first, info = await loader(runner, keys).load(source)
    board_loader = loader(runner, keys)
    await board_loader.load(source)
    again, cached = await board_loader.load(source)

    assert len(first.parts) == 6
    assert not info.cached
    assert cached.cached
    assert again is board_loader._cache[cached.sha256]
    assert runner.calls[0] == ["obv-dump", str(source), "--fz-key", KEY]
    assert len(runner.calls) == 2  # one per loader; the third load came from the cache


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("error_parse_failed.json", "parse_failed"),
        ("error_key_required.json", "key_required"),
        # Real obv-dump output for a file that is not a board: format is null.
        ("error_unknown_format.json", "unknown_format"),
    ],
)
async def test_dump_error_codes(tmp_path: Path, name: str, code: str) -> None:
    source = tmp_path / "board.cad"
    source.write_text("x")
    runner = FakeRunner(lambda _: CommandResult(returncode=1, stdout=fixture(name), stderr=b"log"))

    with pytest.raises(BoardDumpFailed, match=code) as caught:
        await loader(runner).load(source)
    assert caught.value.error.error == code


async def test_crash_and_bad_output(tmp_path: Path) -> None:
    source = tmp_path / "board.cad"
    source.write_text("x")
    crash = FakeRunner(lambda _: CommandResult(returncode=-11, stdout=b"", stderr=b"Segmentation fault"))
    with pytest.raises(BoardLoadError, match=r"crashed \(exit -11\)"):
        await loader(crash).load(source)
    garbage = FakeRunner(lambda _: CommandResult(returncode=0, stdout=b'{"schema_version": 2}', stderr=b""))
    with pytest.raises(BoardLoadError, match=r"does not match docs/boardview-json\.md"):
        await loader(garbage).load(source)


async def test_keys_never_appear_in_errors(tmp_path: Path) -> None:
    source = tmp_path / "board.fz"
    source.write_text("x")

    class TimeoutRunner:
        async def run(self, args: list[str], timeout: timedelta) -> CommandResult:
            raise CommandError(f"command timed out after {timeout}: {' '.join(args)}")

    keys = BoardviewKeys(fz=SecretStr(KEY), cae=SecretStr(KEY), xzz=SecretStr(KEY))
    with pytest.raises(BoardLoadError) as caught:
        await BoardviewLoader(
            TimeoutRunner(), LoaderOptions(dump_bin="obv-dump", timeout=timedelta(1), keys=keys)
        ).load(source)
    assert KEY not in str(caught.value)
    assert caught.value.__cause__ is None
    assert KEY not in repr(keys)


async def test_missing_file() -> None:
    runner = FakeRunner(lambda _: CommandResult(returncode=0, stdout=b"", stderr=b""))
    with pytest.raises(BoardLoadError, match="cannot read"):
        await loader(runner).load(Path("/no/such/board.cad"))
    assert runner.calls == []


# endregion: loader

# region: render


def png_size(png: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(png)) as image:
        return image.size


def test_render_whole_board() -> None:
    png, legend = render_board(board("example.brd.json"), RenderOptions(highlight_parts=["U1"], highlight_nets=["GND"]))

    assert max(png_size(png)) == 1568
    assert (legend.width_px, legend.height_px) == png_size(png)
    assert legend.labels_drawn > 20
    (part,) = legend.highlighted_parts
    assert part.found
    assert part.visible
    assert part.center_px is not None
    assert 0 < part.center_px.x < legend.width_px
    assert legend.highlighted_nets[0].pins_drawn > 0


def test_render_bottom_is_mirrored() -> None:
    tiny = board()
    _, top = render_board(tiny, RenderOptions(side=Side.TOP, highlight_parts=["U2"]))
    _, bottom = render_board(tiny, RenderOptions(side=Side.BOTTOM, highlight_parts=["U2"]))

    assert bottom.mirrored_x
    assert not top.mirrored_x
    top_x = top.highlighted_parts[0].center_px.x
    bottom_x = bottom.highlighted_parts[0].center_px.x
    assert top_x + bottom_x == pytest.approx(top.width_px, abs=2)
    assert not top.highlighted_parts[0].visible
    assert bottom.highlighted_parts[0].visible
    assert any("bottom side" in note for note in top.notes)


def test_render_crop_and_unknown_names() -> None:
    tiny = board()
    _, legend = render_board(tiny, RenderOptions(crop_to_part="R1", highlight_parts=["Q9"], highlight_nets=["NOPE"]))

    assert legend.view_mm.width == pytest.approx(15.0)
    assert not legend.highlighted_parts[0].found
    assert legend.highlighted_nets[0].nets == []
    assert len(legend.notes) == 2
    with pytest.raises(RenderError, match="no part 'Q9'"):
        render_board(tiny, RenderOptions(crop_to_part="Q9"))


# endregion: render
