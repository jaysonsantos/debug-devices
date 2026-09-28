"""Calibration, the profile file, and the `debug-devices-sevenseg` CLI (sevenseg/calibrate.py, profile.py, cli.py)."""

from pathlib import Path

import cv2
import numpy as np
import pytest

from debug_devices_mcp.sevenseg.calibrate import (
    CalibrationError,
    annotate,
    calibrate,
    find_lcd_corners,
    moved_digit_row,
)
from debug_devices_mcp.sevenseg.cli import main
from debug_devices_mcp.sevenseg.decode import read_image
from debug_devices_mcp.sevenseg.profile import LcdLayout, ProfileError, Symbol, load_profile, save_profile
from debug_devices_mcp.sevenseg.reading import LocalStatus
from debug_devices_mcp.sevenseg.template import t21d_layout

from .sevenseg_synth import CORNERS, CROP_SIZE, Degradation, Scene, crop_jpeg, profile_for

LAYOUT = t21d_layout()
SCENE = Scene(" 510", point=1, symbols={Symbol.VOLT, Symbol.DC, Symbol.AUTO})
# The corners of the LCD in the synthetic crop may differ from the drawn ones by the anti-aliased edge.
CORNER_TOLERANCE = 3.0


SOFT = Degradation(blur=0.8, noise=4)


def crop(layout: LcdLayout = LAYOUT) -> np.ndarray:
    return read_image(crop_jpeg(layout, SCENE, SOFT))


# region: calibration


def test_the_lcd_corners_are_found_next_to_a_bright_wall() -> None:
    corners = find_lcd_corners(crop())
    for found, drawn in zip(corners, CORNERS, strict=True):
        assert abs(found.x - drawn.x) <= CORNER_TOLERANCE
        assert abs(found.y - drawn.y) <= CORNER_TOLERANCE


@pytest.mark.parametrize(("dx", "dy"), [(0.0, 0.0), (0.02, -0.02), (-0.03, 0.02)])
def test_the_fit_finds_a_moved_digit_row(dx: float, dy: float) -> None:
    profile, decoded = calibrate(crop(layout=moved_digit_row(LAYOUT, dx, dy, 1.0)))
    assert (profile.image_width, profile.image_height) == CROP_SIZE
    assert (decoded.reading.display_text, decoded.reading.unit, decoded.reading.status) == (
        "5.10",
        "V",
        LocalStatus.READ,
    )


def test_no_lcd_is_an_error() -> None:
    with pytest.raises(CalibrationError, match="no bright quadrilateral"):
        find_lcd_corners(np.full((200, 300, 3), 60, dtype=np.uint8))


def test_an_unknown_template_is_an_error() -> None:
    with pytest.raises(CalibrationError, match="unknown template"):
        calibrate(crop(), template="no-such-meter")


def test_the_annotated_image_has_the_lcd_and_the_crop() -> None:
    image = crop()
    profile, decoded = calibrate(image)
    annotated = annotate(image, profile, decoded)
    assert annotated.ndim == 3
    assert annotated.shape[0] > annotated.shape[1] / profile.layout.aspect


# endregion

# region: profile file


def test_the_profile_round_trip(tmp_path: Path) -> None:
    profile = profile_for(LAYOUT)
    path = tmp_path / "sevenseg" / "profile.json"
    save_profile(profile, path)
    assert load_profile(path) == profile


def test_a_missing_profile_says_to_calibrate(tmp_path: Path) -> None:
    with pytest.raises(ProfileError, match="calibrate"):
        load_profile(tmp_path / "profile.json")


def test_a_broken_profile_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "profile.json"
    path.write_text('{"version": 1}')
    with pytest.raises(ProfileError, match="not valid"):
        load_profile(path)


# endregion

# region: CLI


def test_cli_calibrate_then_read(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    image = tmp_path / "crop.jpg"
    image.write_bytes(crop_jpeg(LAYOUT, SCENE))
    profile = tmp_path / "state" / "profile.json"

    main(["calibrate", "--image", str(image), "--profile", str(profile)])
    assert load_profile(profile).template == "proster-t21d"
    annotated = profile.with_suffix(".annotated.png")
    assert cv2.imread(str(annotated)) is not None
    assert "5.10 V dc_voltage" in capsys.readouterr().out

    main(["read", "--images", str(image), "--images", str(image), "--profile", str(profile)])
    assert "combined: 5.10 V dc_voltage status=read (2/2 frames agree)" in capsys.readouterr().out


# endregion
