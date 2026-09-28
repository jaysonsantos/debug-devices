"""schematic_find, with a small generated PDF (tests/minimal_pdf.py). No real schematic is in the repository.

Run with `uv run pytest mcp/tests/test_schematic.py` in the dev shell: the poppler tests need pdftotext and pdftoppm
(poppler-utils in flake.nix) and skip without them.
"""

import io
import shutil
from datetime import timedelta
from pathlib import Path

import pytest
from mcp import Client
from mcp.types import ImageContent
from PIL import Image

from debug_devices_mcp.config import Settings
from debug_devices_mcp.process import CommandResult, SubprocessRunner
from debug_devices_mcp.schematic import (
    NOT_SET_MESSAGE,
    MatchKind,
    PdfBox,
    SchematicError,
    SchematicFinder,
    SchematicOptions,
    defaults,
    match_kind,
    parse_bbox_xhtml,
)
from debug_devices_mcp.server import build_server

from .conftest import FakeRunner
from .minimal_pdf import PdfText, make_pdf
from .test_server import FakePhone, make_services, no_vision

POPPLER = shutil.which("pdftotext") is not None and shutil.which("pdftoppm") is not None
needs_poppler = pytest.mark.skipif(not POPPLER, reason="poppler-utils (pdftotext, pdftoppm) is not on PATH")
RED_MIN = 200
OTHER_MAX = 60

# Every name is invented. Page 1: a part, its net label, and a longer name that contains R19. Page 2: joined names.
PAGES = [
    [
        PdfText(100, 700, "U7301"),
        PdfText(100, 680, "PP3V3_S5"),
        PdfText(160, 700, "SYN-PMIC"),
        PdfText(300, 400, "R190"),
    ],
    [PdfText(50, 100, "U7301,U7302"), PdfText(400, 600, "R19")],
]

BBOX_XHTML = b"""<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN" "x.dtd">
<html xmlns="http://www.w3.org/1999/xhtml"><head></head><body><doc>
  <page width="612.000000" height="792.000000">
    <word xMin="100.000000" yMin="83.000000" xMax="135.000000" yMax="94.000000">U7301</word>
    <word xMin="100.000000" yMin="103.000000" xMax="158.000000" yMax="114.000000">PP3V3_S5</word>
    <word xMin="162.000000" yMin="103.000000" xMax="170.000000" yMax="114.000000">&amp;</word>
  </page>
</doc></body></html>"""


@pytest.fixture
def pdf(tmp_path: Path) -> Path:
    path = tmp_path / "schematic.pdf"
    path.write_bytes(make_pdf(PAGES))
    return path


def png_bytes(width: int = 40, height: int = 20) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (width, height), (255, 255, 255)).save(output, format="PNG")
    return output.getvalue()


def has_red_outline(png: bytes) -> bool:
    with Image.open(io.BytesIO(png)) as image:
        data = image.convert("RGB").tobytes()
    pixels = zip(data[0::3], data[1::3], data[2::3], strict=True)
    return any(r >= RED_MIN and g <= OTHER_MAX and b <= OTHER_MAX for r, g, b in pixels)


# region: pure parts


@pytest.mark.parametrize(
    ("word", "query", "kind"),
    [
        ("u7301", "u7301", MatchKind.EXACT),
        ("u7301,u7302", "u7302", MatchKind.TOKEN),
        ("pp3v3(s5)", "s5", MatchKind.TOKEN),
        ("r190", "r19", MatchKind.CONTAINS),
        ("pp3v3_s5", "pp3v3", MatchKind.CONTAINS),
        ("c12", "r19", None),
    ],
)
def test_match_kind(word: str, query: str, kind: MatchKind | None) -> None:
    assert match_kind(word, query) is kind


def test_parse_bbox_xhtml() -> None:
    [page] = parse_bbox_xhtml(BBOX_XHTML)
    assert (page.number, page.width, page.height) == (1, 612.0, 792.0)
    assert [word.text for word in page.words] == ["U7301", "PP3V3_S5", "&"]
    assert page.words[0].box == PdfBox(x_min=100, y_min=83, x_max=135, y_max=94)
    assert page.words[0].key == "u7301"


def test_box_grows_inside_the_page() -> None:
    box = PdfBox(x_min=10, y_min=20, x_max=30, y_max=40).grown(72, 100, 50)
    assert box == PdfBox(x_min=0, y_min=0, x_max=100, y_max=50)


async def test_crop_command_and_no_network(pdf: Path) -> None:
    """The fake runner shows the exact poppler commands. Nothing else runs."""

    def respond(args: list[str]) -> CommandResult:
        if args[0] == defaults.PDFTOTEXT:
            return CommandResult(returncode=0, stdout=BBOX_XHTML, stderr=b"")
        # Like pdftoppm: an image of the requested crop size.
        size = int(args[args.index("-W") + 1]), int(args[args.index("-H") + 1])
        return CommandResult(returncode=0, stdout=png_bytes(*size), stderr=b"")

    runner = FakeRunner(respond)
    finder = SchematicFinder(runner, SchematicOptions(path=pdf, crop_dpi=72, margin_pt=40))
    result, images = await finder.find("u7301", 5)

    assert runner.calls[0] == ["pdftotext", "-bbox", str(pdf), "-"]
    # At 72 dpi, one point is one pixel: the box 100..135 x 83..94 grown by 40.
    assert runner.calls[1] == [
        "pdftoppm", "-f", "1", "-l", "1", "-r", "72", "-x", "60", "-y", "43", "-W", "115", "-H", "91",
        "-png", "-singlefile", str(pdf),
    ]  # fmt: skip
    assert {call[0] for call in runner.calls} == {"pdftotext", "pdftoppm"}
    assert result.total_hits == 1
    assert result.hits[0].nearby_text == "PP3V3_S5 &"
    assert len(images) == 1
    assert has_red_outline(images[0])

    # The index is kept while the file does not change.
    await finder.find("PP3V3_S5", 5)
    assert [call[0] for call in runner.calls].count("pdftotext") == 1


async def test_not_set_and_missing(tmp_path: Path) -> None:
    runner = FakeRunner(lambda _: CommandResult(returncode=0, stdout=b"", stderr=b""))
    with pytest.raises(SchematicError, match="DEBUG_DEVICES_SCHEMATIC"):
        await SchematicFinder(runner, SchematicOptions()).find("U1", 5)
    with pytest.raises(SchematicError, match="does not exist"):
        await SchematicFinder(runner, SchematicOptions(path=tmp_path / "none.pdf")).find("U1", 5)
    with pytest.raises(SchematicError, match="without spaces"):
        await SchematicFinder(runner, SchematicOptions(path=tmp_path)).find("U1 U2", 5)
    assert runner.calls == []
    assert "restart" in NOT_SET_MESSAGE


async def test_poppler_failure_is_reported(pdf: Path) -> None:
    runner = FakeRunner(lambda _: CommandResult(returncode=1, stdout=b"", stderr=b"Syntax Error: bad file"))
    with pytest.raises(SchematicError, match="bad file"):
        await SchematicFinder(runner, SchematicOptions(path=pdf)).find("U1", 5)


def test_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DEBUG_DEVICES_SCHEMATIC", "")
    assert Settings.from_cli([]).schematic is None
    monkeypatch.setenv("DEBUG_DEVICES_SCHEMATIC", str(tmp_path / "board.pdf"))
    assert Settings.from_cli([]).schematic == tmp_path / "board.pdf"
    assert Settings.from_cli(["--schematic", "/x/y.pdf"]).schematic == Path("/x/y.pdf")
    assert Settings.from_cli(["--schematic-timeout", "5"]).schematic_timeout == timedelta(seconds=5)


# endregion: pure parts

# region: with poppler


@needs_poppler
async def test_find_exact_and_joined_names(pdf: Path) -> None:
    finder = SchematicFinder(SubprocessRunner(), SchematicOptions(path=pdf))
    result, images = await finder.find("u7301", defaults.MAX_RESULTS)

    assert [(hit.page, hit.text, hit.match) for hit in result.hits] == [
        (1, "U7301", MatchKind.EXACT),
        (2, "U7301,U7302", MatchKind.TOKEN),
    ]
    assert [(page.page, page.hits) for page in result.pages] == [(1, 1), (2, 1)]
    assert "PP3V3_S5" in result.hits[0].nearby_text
    assert "SYN-PMIC" in result.hits[0].nearby_text
    assert result.hits[0].box.x_min == pytest.approx(100, abs=1)
    assert len(images) == 2
    assert all(has_red_outline(png) for png in images)
    assert [hit.crop.image_index if hit.crop else None for hit in result.hits] == [0, 1]


@needs_poppler
async def test_contains_only_without_an_exact_hit(pdf: Path) -> None:
    finder = SchematicFinder(SubprocessRunner(), SchematicOptions(path=pdf, crop_hits=0))
    exact, _ = await finder.find("R19", defaults.MAX_RESULTS)
    loose, images = await finder.find("R1", defaults.MAX_RESULTS)
    missing, _ = await finder.find("Q999", defaults.MAX_RESULTS)

    assert [(hit.page, hit.text) for hit in exact.hits] == [(2, "R19")]
    assert exact.other_contains_hits == 1
    assert {hit.match for hit in loose.hits} == {MatchKind.CONTAINS}
    assert loose.total_hits == len(("R190", "R19"))
    assert images == []
    assert missing.total_hits == 0
    assert "not in the text" in missing.message


@needs_poppler
async def test_tool_and_bench_instructions(settings: Settings, pdf: Path) -> None:
    configured = settings.model_copy(update={"schematic": pdf})
    server = build_server(make_services(configured, FakePhone(), no_vision()))
    async with Client(server) as client:
        found = await client.call_tool("schematic_find", {"query": "PP3V3_S5", "max_results": 5})
        instructions = await client.call_tool("bench_instructions", {})

    assert not found.is_error
    assert found.structured_content is not None
    assert found.structured_content["total_hits"] == 1
    assert found.structured_content["hits"][0]["page"] == 1
    assert "never sends" in found.structured_content["local_only"]
    [image] = [block for block in found.content if isinstance(block, ImageContent)]
    assert image.mime_type == "image/png"
    assert instructions.structured_content is not None
    assert str(pdf) in instructions.structured_content["schematic"]


async def test_tool_without_a_schematic(settings: Settings) -> None:
    server = build_server(make_services(settings.model_copy(update={"schematic": None}), FakePhone(), no_vision()))
    async with Client(server) as client:
        result = await client.call_tool("schematic_find", {"query": "U1"})
        instructions = await client.call_tool("bench_instructions", {})

    assert result.is_error
    assert instructions.structured_content is not None
    assert instructions.structured_content["schematic"] is None


# endregion: with poppler
