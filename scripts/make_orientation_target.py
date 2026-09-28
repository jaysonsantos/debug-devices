#!/usr/bin/env python3
"""Make the orientation test target: an arrow with a notch, and four shapes of different colors in a row.

Print it (or show it on a second screen) and point the phone at it, to check that phone_snapshot shows the same
picture as the monitor preview (docs/reports/dd-ui.md, round 27). The arrow is not mirror-symmetric (the notch), so
a turn and a mirror both show. The drawing is the same as in mcp/tests/test_image_transform.py, scaled.
Same license as this repository.

    uv run python scripts/make_orientation_target.py target.png
"""

import sys
from pathlib import Path

from PIL import Image, ImageDraw

# The test image is 480 x 360; this scale gives 1920 x 1440 for print.
SCALE = 4
BASE_WIDTH, BASE_HEIGHT = 480, 360
BACKGROUND = (245, 245, 245)
BLACK = (0, 0, 0)
SHAPES = (
    ("circle", (220, 30, 30)),
    ("square", (30, 160, 50)),
    ("triangle", (30, 60, 220)),
    ("diamond", (240, 200, 20)),
)
ARROW = [(20, 150), (90, 150), (90, 120), (140, 180), (90, 240), (90, 210), (20, 210)]
NOTCH = (20, 130, 40, 150)
SHAPE_SIZE = 50
SHAPE_LEFT, SHAPE_STEP, SHAPE_TOP, SHAPE_DROP = 190, 72, 150, 60
USAGE = "usage: make_orientation_target.py <output.png>"
# The program name and one output path.
ARGUMENT_COUNT = 2


def scaled(points: list[tuple[int, int]]) -> list[tuple[int, int]]:
    return [(x * SCALE, y * SCALE) for x, y in points]


def draw_target() -> Image.Image:
    image = Image.new("RGB", (BASE_WIDTH * SCALE, BASE_HEIGHT * SCALE), BACKGROUND)
    draw = ImageDraw.Draw(image)
    draw.polygon(scaled(ARROW), fill=BLACK)
    draw.rectangle([value * SCALE for value in NOTCH], fill=BLACK)
    for index, (shape, color) in enumerate(SHAPES):
        x, y = SHAPE_LEFT + index * SHAPE_STEP, SHAPE_TOP + (index % 2) * SHAPE_DROP
        size, half = SHAPE_SIZE, SHAPE_SIZE // 2
        if shape == "circle":
            draw.ellipse(scaled([(x, y), (x + size, y + size)]), fill=color)
        elif shape == "square":
            draw.rectangle(scaled([(x, y), (x + size, y + size)]), fill=color)
        elif shape == "triangle":
            draw.polygon(scaled([(x, y + size), (x + half, y), (x + size, y + size)]), fill=color)
        else:
            draw.polygon(scaled([(x + half, y), (x + size, y + half), (x + half, y + size), (x, y + half)]), fill=color)
    return image


def main() -> int:
    if len(sys.argv) != ARGUMENT_COUNT:
        print(USAGE, file=sys.stderr)
        return 1
    draw_target().save(Path(sys.argv[1]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
