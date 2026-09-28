"""Synthetic 7-segment LCD crops for the sevenseg tests. The tests draw every image: no real frames in the repo."""

from dataclasses import dataclass, field
from datetime import UTC, datetime

import cv2
import numpy as np
from numpy.typing import NDArray

from debug_devices_mcp.sevenseg.constants import TEMPLATE_T21D
from debug_devices_mcp.sevenseg.decode import BLANK, digit_polygon, rect_polygon
from debug_devices_mcp.sevenseg.profile import LcdLayout, MeterProfile, Point, Rect, Segment, Symbol

Image = NDArray[np.uint8]

SEGMENTS_OF = {
    "0": "abcdef",
    "1": "bc",
    "2": "abdeg",
    "3": "abcdg",
    "4": "bcfg",
    "5": "acdfg",
    "6": "acdefg",
    "7": "abc",
    "8": "abcdefg",
    "9": "abcdfg",
    "-": "g",
    "L": "def",
    BLANK: "",
}
# The full stroke of each segment in digit-box coordinates (the template regions are inner parts of them).
STROKES = {
    Segment.A: Rect(x=0.12, y=0.0, width=0.76, height=0.1),
    Segment.B: Rect(x=0.82, y=0.06, width=0.18, height=0.41),
    Segment.C: Rect(x=0.82, y=0.53, width=0.18, height=0.41),
    Segment.D: Rect(x=0.12, y=0.9, width=0.76, height=0.1),
    Segment.E: Rect(x=0.0, y=0.53, width=0.18, height=0.41),
    Segment.F: Rect(x=0.0, y=0.06, width=0.18, height=0.41),
    Segment.G: Rect(x=0.12, y=0.45, width=0.76, height=0.1),
}
# Hershey fonts have no Ω or µ: the glyph shape does not matter, only its ink in the box.
GLYPHS = {
    Symbol.AUTO: "AUTO",
    Symbol.HOLD: "HOLD",
    Symbol.AC: "AC",
    Symbol.DC: "DC",
    Symbol.DIODE: ">|",
    Symbol.CONTINUITY: ")))",
    Symbol.LOW_BATTERY: "[+]",
    Symbol.REL: "REL",
    Symbol.NANO: "n",
    Symbol.MICRO: "u",
    Symbol.MILLI: "m",
    Symbol.KILO: "k",
    Symbol.MEGA: "M",
    Symbol.VOLT: "V",
    Symbol.AMPERE: "A",
    Symbol.OHM: "O",
    Symbol.FARAD: "F",
    Symbol.HERTZ: "Hz",
    Symbol.PERCENT: "%",
    Symbol.CELSIUS: "C",
    Symbol.FAHRENHEIT: "F",
}
SUPERSAMPLE = 4
LCD_HEIGHT = 160
PAPER = 190
INK = 45
BODY = 70
WALL = 225
CROP_SIZE = (420, 300)
# A small perspective change: the LCD corners in the crop (top left, top right, bottom right, bottom left).
CORNERS = (Point(x=52, y=70), Point(x=372, y=58), Point(x=378, y=212), Point(x=46, y=226))
FRONT_CORNERS = (Point(x=50, y=64), Point(x=372, y=64), Point(x=372, y=210), Point(x=50, y=210))
BODY_MARGIN = 28


@dataclass
class Scene:
    """What the LCD shows: one character per digit position, the point after digit `point`, and the symbols."""

    chars: str
    point: int | None = None
    negative: bool = False
    symbols: set[Symbol] = field(default_factory=set)
    paper: int = PAPER
    ink: int = INK


def to_pixels(polygon: tuple[tuple[float, float], ...], width: int, height: int) -> NDArray[np.int32]:
    return np.array([(x * width, y * height) for x, y in polygon], dtype=np.float64).round().astype(np.int32)


def draw_glyph(canvas: Image, text: str, rect: Rect, ink: int) -> None:
    height, width = canvas.shape
    box_width, box_height = rect.width * width, rect.height * height
    font = cv2.FONT_HERSHEY_SIMPLEX
    (text_width, text_height), _ = cv2.getTextSize(text, font, 1.0, 1)
    scale = min(0.9 * box_width / text_width, 0.8 * box_height / text_height)
    # LCD symbols are bold: at least 1.5 pixels after the scale-down.
    thickness = max(round(1.5 * SUPERSAMPLE), round(scale * 2.5))
    (text_width, text_height), _ = cv2.getTextSize(text, font, scale, thickness)
    origin = (
        round(rect.x * width + (box_width - text_width) / 2),
        round(rect.y * height + (box_height + text_height) / 2),
    )
    cv2.putText(canvas, text, origin, font, scale, ink, thickness, cv2.LINE_AA)


def render_lcd(layout: LcdLayout, scene: Scene, height: int = LCD_HEIGHT) -> Image:
    """The LCD seen from the front, drawn at SUPERSAMPLE times the size, then scaled down (smooth edges)."""
    big_height = height * SUPERSAMPLE
    big_width = round(big_height * layout.aspect)
    canvas = np.full((big_height, big_width), scene.paper, dtype=np.uint8)
    for index, char in enumerate(scene.chars.ljust(len(layout.digits))[: len(layout.digits)]):
        box = layout.digits[index]
        for letter in SEGMENTS_OF[char]:
            polygon = digit_polygon(box, STROKES[Segment(letter)], layout.slant)
            cv2.fillPoly(canvas, [to_pixels(polygon, big_width, big_height)], scene.ink)
    if scene.point is not None:
        cv2.fillPoly(canvas, [to_pixels(rect_polygon(layout.points[scene.point]), big_width, big_height)], scene.ink)
    if scene.negative:
        cv2.fillPoly(canvas, [to_pixels(rect_polygon(layout.sign), big_width, big_height)], scene.ink)
    for symbol in scene.symbols:
        draw_glyph(canvas, GLYPHS[symbol], layout.symbols[symbol], scene.ink)
    return cv2.resize(canvas, (round(height * layout.aspect), height), interpolation=cv2.INTER_AREA)


def place_on_crop(
    lcd: Image, corners: tuple[Point, ...] = CORNERS, size: tuple[int, int] = CROP_SIZE, wall: bool = True
) -> Image:
    """The LCD inside a dark meter body, with a bright wall at the top and the left (it touches the crop border)."""
    width, height = size
    crop = np.full((height, width), BODY, dtype=np.uint8)
    if wall:
        crop[: BODY_MARGIN // 2, :] = WALL
        crop[:, : BODY_MARGIN // 2] = WALL
    lcd_height, lcd_width = lcd.shape
    source = np.array([(0, 0), (lcd_width - 1, 0), (lcd_width - 1, lcd_height - 1), (0, lcd_height - 1)], np.float32)
    target = np.array([(point.x, point.y) for point in corners], dtype=np.float32)
    matrix = cv2.getPerspectiveTransform(source, target)
    warped = cv2.warpPerspective(lcd, matrix, (width, height), flags=cv2.INTER_LINEAR)
    mask = cv2.warpPerspective(np.full_like(lcd, 255), matrix, (width, height), flags=cv2.INTER_LINEAR)
    alpha = mask.astype(np.float32) / 255
    return (warped * alpha + crop * (1 - alpha)).round().astype(np.uint8)


@dataclass(frozen=True)
class Degradation:
    """Blur (Gaussian sigma in pixels), noise (sigma in gray levels), and a soft bright glare spot (its peak)."""

    blur: float = 0.0
    noise: float = 0.0
    glare: float = 0.0
    glare_at: tuple[float, float] = (0.6, 0.35)
    seed: int = 7


def degrade(image: Image, degradation: Degradation) -> Image:
    result = image.astype(np.float32)
    if degradation.blur:
        result = cv2.GaussianBlur(result, (0, 0), degradation.blur)
    if degradation.glare:
        height, width = image.shape
        ys, xs = np.mgrid[0:height, 0:width]
        cx, cy = degradation.glare_at[0] * width, degradation.glare_at[1] * height
        sigma = 0.12 * width
        result += degradation.glare * np.exp(-((xs - cx) ** 2 + (ys - cy) ** 2) / (2 * sigma**2))
    if degradation.noise:
        result += np.random.default_rng(degradation.seed).normal(0, degradation.noise, image.shape)
    return np.clip(result, 0, 255).round().astype(np.uint8)


def jpeg(image: Image, quality: int = 90) -> bytes:
    """The crop as the webcam gives it: a color JPEG."""
    ok, data = cv2.imencode(".jpg", cv2.cvtColor(image, cv2.COLOR_GRAY2BGR), [cv2.IMWRITE_JPEG_QUALITY, quality])
    assert ok
    return data.tobytes()


def profile_for(
    layout: LcdLayout, corners: tuple[Point, ...] = CORNERS, size: tuple[int, int] = CROP_SIZE
) -> MeterProfile:
    """The profile that a perfect calibration of this synthetic crop gives."""
    return MeterProfile(
        template=TEMPLATE_T21D,
        image_width=size[0],
        image_height=size[1],
        corners=list(corners),
        layout=layout,
        calibrated_at=datetime.now(UTC),
    )


CLEAN = Degradation()


def crop_jpeg(layout: LcdLayout, scene: Scene, degradation: Degradation = CLEAN) -> bytes:
    return jpeg(degrade(place_on_crop(render_lcd(layout, scene)), degradation))
