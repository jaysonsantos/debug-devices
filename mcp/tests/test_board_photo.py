"""Homography fit and the photo tools (board_register_photo, board_locate_in_photo)."""

import base64
import io
from pathlib import Path

import numpy as np
import pytest
from mcp import Client
from mcp.types import ImageContent, TextContent
from PIL import Image

from debug_devices_mcp.board.homography import HomographyError, apply, fit_homography, likely_outlier
from debug_devices_mcp.config import Settings

from .test_board import board, fixture
from .test_board_tools import server_with

# A perspective view that also mirrors X (as a photo of the bottom side does).
TRUE_MATRIX = np.array([[-12.0, 1.5, 700.0], [0.8, -11.0, 500.0], [0.0004, 0.0003, 1.0]])
PHOTO_SIZE = (800, 600)


def photo_points(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    return [(float(x), float(y)) for x, y in apply(TRUE_MATRIX, np.asarray(points))]


BOARD_POINTS = [(0.0, 0.0), (50.0, 0.0), (50.0, 25.0), (0.0, 25.0), (20.0, 10.0), (35.0, 18.0)]


def test_fit_recovers_the_matrix() -> None:
    fit = fit_homography(BOARD_POINTS, photo_points(BOARD_POINTS))

    matrix = np.asarray(fit.matrix)
    assert np.allclose(matrix / matrix[2, 2], TRUE_MATRIX, rtol=1e-6, atol=1e-8)
    assert fit.max_error_px < 1e-6
    assert fit.error_limit_px is not None


def test_four_pairs_are_exact_and_not_checked() -> None:
    fit = fit_homography(BOARD_POINTS[:4], photo_points(BOARD_POINTS[:4]))
    assert fit.error_limit_px is None
    assert fit.max_error_px < 1e-6


def test_outlier_gives_a_large_error_and_is_found() -> None:
    photo = photo_points(BOARD_POINTS)
    photo[4] = (photo[4][0] + 60, photo[4][1])
    fit = fit_homography(BOARD_POINTS, photo)
    assert fit.error_limit_px is not None
    assert fit.max_error_px > fit.error_limit_px
    assert likely_outlier(BOARD_POINTS, photo) == 4
    # With 5 pairs, any 4 fit exactly: the wrong pair cannot be found.
    assert likely_outlier(BOARD_POINTS[:5], photo[:5]) is None


@pytest.mark.parametrize(
    "points",
    [BOARD_POINTS[:3], [(0.0, 0.0), (1.0, 0.0), (2.0, 0.0), (3.0, 0.0)], [(1.0, 1.0)] * 4],
)
def test_bad_pairs(points: list[tuple[float, float]]) -> None:
    with pytest.raises(HomographyError):
        fit_homography(points, [(float(index), float(index * index)) for index in range(len(points))])


def centers(names: list[str]) -> list[tuple[float, float]]:
    tiny = board()
    return [(tiny.parts[name].center.x, tiny.parts[name].center.y) for name in names]


async def test_register_and_locate(settings: Settings, tmp_path: Path) -> None:
    board_file = tmp_path / "tiny.cad"
    board_file.write_text("x")
    names = ["U1", "R1", "TP1", "J1", "U2", "C1"]
    pairs = [
        {"refdes": name, "x_px": x, "y_px": y} for name, (x, y) in zip(names, photo_points(centers(names)), strict=True)
    ]
    # The annotated photo can be larger than the registered size (for example the full-size saved snapshot).
    photo = tmp_path / "photo.jpg"
    Image.new("RGB", (PHOTO_SIZE[0] * 2, PHOTO_SIZE[1] * 2), (40, 90, 40)).save(photo)
    server, _ = server_with(settings, fixture("tiny.json"))
    size = {"photo_width_px": PHOTO_SIZE[0], "photo_height_px": PHOTO_SIZE[1]}

    async with Client(server) as client:
        await client.call_tool("board_open", {"path": str(board_file)})
        registered = await client.call_tool("board_register_photo", {"side": "top", "pairs": pairs, **size})
        assert registered.structured_content is not None, registered.content
        registration_id = registered.structured_content["registration_id"]
        located = await client.call_tool(
            "board_locate_in_photo",
            {
                "registration_id": registration_id,
                "refdes": ["U1", "C1", "Q9"],
                "net": "PP3V3",
                "photo_path": str(photo),
            },
        )
        bad_pairs = [*pairs[:4], {**pairs[4], "x_px": pairs[4]["x_px"] + 80}, pairs[5]]
        refused = await client.call_tool("board_register_photo", {"side": "top", "pairs": bad_pairs, **size})
        unknown = await client.call_tool("board_locate_in_photo", {"registration_id": "nope", "refdes": ["U1"]})

    assert registered.structured_content["checked"] is True
    assert registered.structured_content["fit"]["max_error_px"] < 1e-3
    result = located.structured_content
    assert result is not None
    (expected,) = photo_points(centers(["U1"]))
    u1 = result["parts"][0]
    assert (u1["x_px"], u1["y_px"]) == (pytest.approx(expected[0], abs=0.1), pytest.approx(expected[1], abs=0.1))
    assert u1["on_registered_side"] is True
    assert result["parts"][1]["name"] == "C1"
    assert result["parts"][1]["on_registered_side"] is False
    assert result["notes"] == ["part 'Q9': not on this board"]
    # PP3V3 pins: U1 pin 1 and R1 pin 2 are on the top; U2 pin 1 is on the bottom and is left out.
    assert sorted((pin["part"], pin["number"]) for pin in result["pins"]) == [("R1", "2"), ("U1", "1")]
    assert result["annotated"] is True
    image = next(block for block in located.content if isinstance(block, ImageContent))
    with Image.open(io.BytesIO(base64.b64decode(image.data))) as annotated:
        assert annotated.size == (1568, 1176)
    assert refused.is_error
    assert (
        "most likely wrong pair is U2"
        in next(block for block in refused.content if isinstance(block, TextContent)).text
    )
    assert unknown.is_error
