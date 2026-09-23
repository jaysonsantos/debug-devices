#!/usr/bin/env python3
"""Compose the demo meter photo: a webcam frame of the multimeter with a drawn 7-segment reading.

The README demo uses a fake phone that serves this photo as its snapshot. Only the meter area of the frame goes
into the photo. Check the output: it must not show people. Run it from the repo root while a monitor runs:

    uv run python scripts/make_demo_meter.py
    uv run python scripts/make_demo_meter.py --text 4.70 --unit kΩ --output docs/images/demo-meter.jpg
"""

from __future__ import annotations

import argparse
import io
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

# region: constants

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FRAME_URL = "http://127.0.0.1:18766/api/webcam/frame.jpg"
DEFAULT_OUTPUT = REPO_ROOT / "docs" / "images" / "demo-meter.jpg"
# Boxes in the 1920x1080 webcam frame: x0,y0,x1,y1.
DEFAULT_METER_BOX = "870,60,1340,860"
DEFAULT_LCD_BOX = "975,150,1245,290"
DEFAULT_TEXT = "4.70"
DEFAULT_UNIT = "kΩ"
UNIT_FONT = Path("/usr/share/fonts/TTF/DejaVuSans-Bold.ttf")
FRAME_TIMEOUT_SECONDS = 10
JPEG_QUALITY = 92
BOX_PARTS = 4

# LCD ink: a dark grey-green, not pure black. Alpha keeps some of the LCD texture.
INK = (38, 44, 40, 235)
INK_BLUR_RADIUS = 0.7
# Digit geometry as fractions of the LCD height.
DIGIT_HEIGHT = 0.56
# Where the free height goes: 0.6 puts the digits a little below the middle, under the flags.
DIGIT_TOP = 0.6
DIGIT_WIDTH = 0.3
SEGMENT_THICKNESS = 0.07
DIGIT_GAP = 0.1
# Classic LCD digits lean to the right.
SLANT = 0.08
MARGIN_LEFT = 0.1
DOT_SIZE = 0.11
# The small gap between two segments, as a fraction of the segment thickness.
SEGMENT_GAP = 0.15
FLAG_TOP = 0.3
UNIT_SIZE = 0.26
FLAG_SIZE = 0.13
FLAG_TEXT = "AUTO"

# Segments a-g for each digit. a top, b top right, c bottom right, d bottom, e bottom left, f top left, g middle.
SEGMENTS = {
    "0": "abcdef",
    "1": "bc",
    "2": "abged",
    "3": "abgcd",
    "4": "fgbc",
    "5": "afgcd",
    "6": "afgedc",
    "7": "abc",
    "8": "abcdefg",
    "9": "abcdfg",
    "-": "g",
}
DECIMAL_POINT = "."

# endregion: constants


@dataclass(frozen=True)
class Box:
    x0: int
    y0: int
    x1: int
    y1: int

    @classmethod
    def parse(cls, text: str) -> Box:
        parts = [int(part) for part in text.split(",")]
        if len(parts) != BOX_PARTS:
            raise argparse.ArgumentTypeError(f"box must be x0,y0,x1,y1, not {text!r}")
        return cls(*parts)

    @property
    def width(self) -> int:
        return self.x1 - self.x0

    @property
    def height(self) -> int:
        return self.y1 - self.y0


def fetch_frame(url: str) -> Image.Image:
    with urllib.request.urlopen(url, timeout=FRAME_TIMEOUT_SECONDS) as response:
        return Image.open(io.BytesIO(response.read())).convert("RGB")


def horizontal(x_start: float, x_end: float, y: float, thick: float) -> list[tuple[float, float]]:
    half = thick / 2
    return [
        (x_start, y),
        (x_start + half, y - half),
        (x_end - half, y - half),
        (x_end, y),
        (x_end - half, y + half),
        (x_start + half, y + half),
    ]


def vertical(x: float, y_start: float, y_end: float, thick: float) -> list[tuple[float, float]]:
    half = thick / 2
    return [
        (x, y_start),
        (x + half, y_start + half),
        (x + half, y_end - half),
        (x, y_end),
        (x - half, y_end - half),
        (x - half, y_start + half),
    ]


def segment_polygons(left: float, top: float, width: float, height: float, thick: float) -> dict[str, list]:
    """Classic hexagonal segments of one digit, before the slant, with a small gap at each joint."""
    gap = thick * SEGMENT_GAP
    x_left, x_right = left + thick / 2, left + width - thick / 2
    y_top, y_middle, y_bottom = top + thick / 2, top + height / 2, top + height - thick / 2
    return {
        "a": horizontal(x_left + gap, x_right - gap, y_top, thick),
        "g": horizontal(x_left + gap, x_right - gap, y_middle, thick),
        "d": horizontal(x_left + gap, x_right - gap, y_bottom, thick),
        "f": vertical(x_left, y_top + gap, y_middle - gap, thick),
        "e": vertical(x_left, y_middle + gap, y_bottom - gap, thick),
        "b": vertical(x_right, y_top + gap, y_middle - gap, thick),
        "c": vertical(x_right, y_middle + gap, y_bottom - gap, thick),
    }


def slanted(points: list[tuple[float, float]], baseline: float, slant: float) -> list[tuple[float, float]]:
    return [(x + (baseline - y) * slant, y) for x, y in points]


def draw_reading(size: tuple[int, int], lcd: Box, text: str, unit: str) -> Image.Image:
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    digit_height = lcd.height * DIGIT_HEIGHT
    digit_width = lcd.height * DIGIT_WIDTH
    thick = lcd.height * SEGMENT_THICKNESS
    top = lcd.y0 + (lcd.height - digit_height) * DIGIT_TOP
    baseline = top + digit_height
    x = lcd.x0 + lcd.width * MARGIN_LEFT
    for char in text:
        if char == DECIMAL_POINT:
            # The point gets its own narrow place on the baseline, between the digits.
            dot = lcd.height * DOT_SIZE
            draw.rectangle([x, baseline - dot, x + dot, baseline], fill=INK)
            x += dot + lcd.height * DIGIT_GAP
            continue
        polygons = segment_polygons(x, top, digit_width, digit_height, thick)
        for name in SEGMENTS[char]:
            draw.polygon(slanted(polygons[name], baseline, SLANT), fill=INK)
        x += digit_width + lcd.height * DIGIT_GAP
    unit_font = ImageFont.truetype(str(UNIT_FONT), int(lcd.height * UNIT_SIZE))
    draw.text((x + lcd.height * DIGIT_GAP, baseline), unit, font=unit_font, fill=INK, anchor="ls")
    flag_font = ImageFont.truetype(str(UNIT_FONT), int(lcd.height * FLAG_SIZE))
    flag_position = (lcd.x0 + lcd.width * MARGIN_LEFT, lcd.y0 + lcd.height * FLAG_TOP)
    draw.text(flag_position, FLAG_TEXT, font=flag_font, fill=INK, anchor="ls")
    return layer.filter(ImageFilter.GaussianBlur(INK_BLUR_RADIUS))


def compose(frame: Image.Image, meter: Box, lcd: Box, text: str, unit: str) -> Image.Image:
    photo = frame.crop((meter.x0, meter.y0, meter.x1, meter.y1)).convert("RGBA")
    local_lcd = Box(lcd.x0 - meter.x0, lcd.y0 - meter.y0, lcd.x1 - meter.x0, lcd.y1 - meter.y0)
    return Image.alpha_composite(photo, draw_reading(photo.size, local_lcd, text, unit)).convert("RGB")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=DEFAULT_FRAME_URL, help="full webcam frame of a running monitor")
    parser.add_argument("--frame", type=Path, help="use this JPEG instead of the URL")
    parser.add_argument("--meter-box", type=Box.parse, default=Box.parse(DEFAULT_METER_BOX))
    parser.add_argument("--lcd-box", type=Box.parse, default=Box.parse(DEFAULT_LCD_BOX))
    parser.add_argument("--text", default=DEFAULT_TEXT)
    parser.add_argument("--unit", default=DEFAULT_UNIT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    frame = Image.open(args.frame).convert("RGB") if args.frame else fetch_frame(args.url)
    photo = compose(frame, args.meter_box, args.lcd_box, args.text, args.unit)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    photo.save(args.output, quality=JPEG_QUALITY)
    print(f"{args.output}: {photo.width}x{photo.height}")


if __name__ == "__main__":
    main()
