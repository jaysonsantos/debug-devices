"""Decode one LCD frame: warp, normalize, score each region, then read the digits, the point, the sign, and the symbols.

The LCD shows dark segments on a light background. The background estimate (a morphological close that removes the
thin dark strokes) makes the darkness of a pixel relative to its surroundings, so uneven light and mild glare cancel.
The ink threshold adapts to each frame: half of the darkest ink in the regions, and at least the Otsu split.
"""

import time
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

import cv2
import numpy as np
from numpy.typing import NDArray
from pydantic import BaseModel

from debug_devices_mcp.multimeter import MeterMode, is_overload, signed_value
from debug_devices_mcp.sevenseg.constants import score, warp
from debug_devices_mcp.sevenseg.profile import LcdLayout, MeterProfile, Rect, Segment, Symbol
from debug_devices_mcp.sevenseg.reading import LocalReading, LocalStatus
from debug_devices_mcp.sevenseg.symbols import DECIDING, flags_of, mode_of, unit_of

type Gray = NDArray[np.uint8]
type Darkness = NDArray[np.float32]

# region: regions

SIGN_NAME = "sign"
OVERLOAD_TEXT = "OL"
BLANK = " "
UNKNOWN_CHAR = "?"
DIGIT_CHARS = frozenset("0123456789")
UINT8_MAX = 255
GRAY_DIMENSIONS = 2


class RegionKind(StrEnum):
    SEGMENT = "segment"
    POINT = "point"
    SIGN = "sign"
    SYMBOL = "symbol"


def segment_name(digit: int, segment: Segment) -> str:
    return f"digit{digit}.{segment}"


def point_name(index: int) -> str:
    return f"point{index}"


def symbol_name(symbol: Symbol) -> str:
    return f"symbol.{symbol}"


@dataclass(frozen=True)
class Region:
    """One region of the LCD as a polygon in normalized LCD coordinates."""

    name: str
    kind: RegionKind
    polygon: tuple[tuple[float, float], ...]
    digit: int | None = None
    segment: Segment | None = None
    symbol: Symbol | None = None


def rect_polygon(rect: Rect) -> tuple[tuple[float, float], ...]:
    return (
        (rect.x, rect.y),
        (rect.x + rect.width, rect.y),
        (rect.x + rect.width, rect.y + rect.height),
        (rect.x, rect.y + rect.height),
    )


def digit_polygon(box: Rect, rect: Rect, slant: float) -> tuple[tuple[float, float], ...]:
    """A segment rectangle in digit-box coordinates, slanted: the top of the digit is `slant` box widths right."""
    return tuple((box.x + box.width * (u + slant * (1.0 - v)), box.y + box.height * v) for u, v in rect_polygon(rect))


def layout_regions(layout: LcdLayout) -> list[Region]:
    regions = [
        Region(
            segment_name(index, segment),
            RegionKind.SEGMENT,
            digit_polygon(box, rect, layout.slant),
            digit=index,
            segment=segment,
        )
        for index, box in enumerate(layout.digits)
        for segment, rect in layout.segments.items()
    ]
    regions += [
        Region(point_name(index), RegionKind.POINT, rect_polygon(rect)) for index, rect in enumerate(layout.points)
    ]
    regions.append(Region(SIGN_NAME, RegionKind.SIGN, rect_polygon(layout.sign)))
    regions += [
        Region(symbol_name(symbol), RegionKind.SYMBOL, rect_polygon(rect), symbol=symbol)
        for symbol, rect in layout.symbols.items()
    ]
    return regions


# endregion: regions

# region: image


def warp_size(layout: LcdLayout) -> tuple[int, int]:
    return max(warp.MIN_WIDTH, round(warp.HEIGHT * layout.aspect)), warp.HEIGHT


def to_gray(image: NDArray[np.uint8]) -> Gray:
    return image if image.ndim == GRAY_DIMENSIONS else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)


def warp_lcd(gray: Gray, profile: MeterProfile) -> Gray:
    """The LCD, seen from the front, at a fixed size."""
    width, height = warp_size(profile.layout)
    source = np.array([(point.x, point.y) for point in profile.corners], dtype=np.float32)
    target = np.array([(0, 0), (width - 1, 0), (width - 1, height - 1), (0, height - 1)], dtype=np.float32)
    matrix = cv2.getPerspectiveTransform(source, target)
    return cv2.warpPerspective(gray, matrix, (width, height), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)


def odd(value: float) -> int:
    size = max(3, round(value))
    return size if size % 2 else size + 1


def darkness_map(lcd: Gray) -> Darkness:
    """0 for the background, up to 1 for black: the darkness of each pixel relative to the local background."""
    height = lcd.shape[0]
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (odd(height * score.BACKGROUND_KERNEL_FRACTION),) * 2)
    background = cv2.morphologyEx(lcd, cv2.MORPH_CLOSE, kernel).astype(np.float32)
    background = cv2.GaussianBlur(background, (0, 0), height * score.BACKGROUND_BLUR_FRACTION)
    darkness = 1.0 - lcd.astype(np.float32) / np.maximum(background, 1.0)
    return np.clip(darkness, 0.0, 1.0)


def region_mask(region: Region, width: int, height: int) -> tuple[tuple[slice, slice], NDArray[np.bool_]]:
    """The pixel box of a region and its polygon mask inside that box."""
    points = np.array([(x * width, y * height) for x, y in region.polygon], dtype=np.float32)
    left, top = np.floor(points.min(axis=0)).astype(int)
    right, bottom = np.ceil(points.max(axis=0)).astype(int)
    left, top = max(left, 0), max(top, 0)
    right, bottom = min(right, width), min(bottom, height)
    if right <= left or bottom <= top:
        return (slice(0, 0), slice(0, 0)), np.zeros((0, 0), dtype=bool)
    mask = np.zeros((bottom - top, right - left), dtype=np.uint8)
    cv2.fillPoly(mask, [np.round(points - (left, top)).astype(np.int32)], 1)
    return (slice(top, bottom), slice(left, right)), mask.astype(bool)


# endregion: image

# region: scores


class RegionScore(BaseModel):
    name: str
    # The part of the region's pixels that is ink.
    fill: float
    on: bool
    # 0..1: how far the fill is from the on/off threshold.
    confidence: float


def ink_threshold(values: NDArray[np.float32]) -> tuple[float, float]:
    """The ink threshold of a frame and its contrast (the darkness of the darkest ink)."""
    if values.size == 0:
        return 1.0, 0.0
    contrast = float(np.percentile(values, score.INK_PERCENTILE))
    scaled = np.clip(values * UINT8_MAX, 0, UINT8_MAX).astype(np.uint8).reshape(1, -1)
    otsu, _ = cv2.threshold(scaled, 0, UINT8_MAX, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return max(otsu / UINT8_MAX, contrast * score.INK_THRESHOLD_FRACTION), contrast


def score_regions(darkness: Darkness, regions: list[Region]) -> tuple[list[RegionScore], float]:
    """The fill of each region at the adaptive threshold of this frame, and the frame contrast."""
    height, width = darkness.shape
    masks = [region_mask(region, width, height) for region in regions]
    values = [darkness[box][mask] for box, mask in masks]
    threshold, contrast = ink_threshold(np.concatenate(values) if values else np.zeros(0, dtype=np.float32))
    scores = []
    for region, pixels in zip(regions, values, strict=True):
        # A symbol is a thin glyph in its box: it fills less of the box than a segment fills its inner part, and
        # blur makes its thin strokes lighter than a wide segment.
        symbol = region.kind is RegionKind.SYMBOL
        fill_on, margin = (
            (score.SYMBOL_FILL_ON, score.SYMBOL_FILL_MARGIN) if symbol else (score.FILL_ON, score.FILL_MARGIN)
        )
        level = threshold * score.SYMBOL_THRESHOLD_FRACTION if symbol else threshold
        fill = float(np.mean(pixels > level)) if pixels.size else 0.0
        confidence = min(1.0, abs(fill - fill_on) / margin) if pixels.size else 0.0
        scores.append(RegionScore(name=region.name, fill=round(fill, 3), on=fill >= fill_on, confidence=confidence))
    return scores, contrast


# endregion: scores

# region: digits


def pattern(segments: str) -> frozenset[Segment]:
    return frozenset(Segment(letter) for letter in segments)


# Digits first: a letter never replaces a digit pattern. "O" of "OL" is the "0" pattern.
PATTERNS: dict[frozenset[Segment], str] = {
    pattern(""): BLANK,
    pattern("abcdef"): "0",
    pattern("bc"): "1",
    pattern("abdeg"): "2",
    pattern("abcdg"): "3",
    pattern("bcfg"): "4",
    pattern("acdfg"): "5",
    pattern("acdefg"): "6",
    pattern("cdefg"): "6",
    pattern("abc"): "7",
    pattern("abcf"): "7",
    pattern("abcdefg"): "8",
    pattern("abcdfg"): "9",
    pattern("abcfg"): "9",
    pattern("g"): "-",
    pattern("def"): "L",
    pattern("adefg"): "E",
    pattern("aefg"): "F",
    pattern("abefg"): "P",
    pattern("ceg"): "n",
    pattern("eg"): "r",
    pattern("defg"): "t",
    pattern("adef"): "C",
    pattern("bcefg"): "H",
    pattern("abcefg"): "A",
    pattern("bcdef"): "U",
    pattern("cdeg"): "o",
    pattern("cde"): "u",
}


def decode_digit(on: frozenset[Segment]) -> tuple[str, bool]:
    """The character of a segment pattern, and whether the match is exact. One segment off gives the only pattern
    at that distance (not exact); otherwise "?"."""
    if on in PATTERNS:
        return PATTERNS[on], True
    near = {PATTERNS[known] for known in PATTERNS if len(known ^ on) <= score.NEAREST_MAX_DISTANCE}
    if len(near) == 1:
        return near.pop(), False
    return UNKNOWN_CHAR, False


# endregion: digits

# region: frame


class DecodedFrame(BaseModel):
    """One decoded frame with the details for calibration and evaluation."""

    reading: LocalReading
    # The character of each digit position (" " for blank).
    chars: list[str]
    regions: list[RegionScore]
    elapsed: timedelta


def unreadable(problem: str, contrast: float = 0.0) -> LocalReading:
    return LocalReading(
        status=LocalStatus.UNREADABLE,
        readable=False,
        display_text="",
        digits=None,
        digits_before_point=None,
        negative=False,
        value=None,
        unit="unknown",
        mode=MeterMode.OTHER,
        flags=[],
        overload=False,
        confidence=0.0,
        contrast=round(contrast, 3),
        weak_regions=[],
        problems=[problem],
    )


def display_of(chars: list[str], point: int | None, negative: bool) -> str:
    text = "".join(char + ("." if index == point else "") for index, char in enumerate(chars)).strip()
    return ("-" if negative else "") + text


def digit_problems(chars: list[str]) -> list[str]:
    shown = [index for index, char in enumerate(chars) if char != BLANK]
    problems = []
    if shown and any(chars[index] == BLANK for index in range(shown[0], shown[-1] + 1)):
        problems.append("a blank digit between digits")
    if UNKNOWN_CHAR in chars:
        problems.append("a digit pattern is unknown")
    return problems


def build_reading(
    regions: list[Region], scores: list[RegionScore], layout: LcdLayout, contrast: float
) -> tuple[LocalReading, list[str]]:
    by_name = {item.name: item for item in scores}
    problems: list[str] = []
    chars = []
    for index in range(len(layout.digits)):
        on = frozenset(segment for segment in Segment if by_name[segment_name(index, segment)].on)
        char, exact = decode_digit(on)
        if not exact and char != UNKNOWN_CHAR:
            problems.append(f"digit {index + 1} read as {char!r} with one unclear segment")
        chars.append(char)
    points_on = [index for index in range(len(layout.points)) if by_name[point_name(index)].on]
    if len(points_on) > 1:
        problems.append(f"{len(points_on)} decimal points are on")
    point = max(points_on, key=lambda index: by_name[point_name(index)].fill) if points_on else None
    negative = by_name[SIGN_NAME].on
    symbols_on = {region.symbol for region in regions if region.symbol is not None and by_name[region.name].on}
    unit, unit_problems = unit_of(symbols_on)
    problems += unit_problems + digit_problems(chars)
    display_text = display_of(chars, point, negative)
    overload = is_overload(display_text)
    shown = [char for char in chars if char != BLANK]
    numeric = bool(shown) and all(char in DIGIT_CHARS for char in shown) and not overload
    digits = "".join(shown) if numeric else None
    before = sum(1 for char in chars[: point + 1] if char in DIGIT_CHARS) if numeric and point is not None else None
    if not shown:
        problems.append("the LCD shows no digits")
    elif not numeric and not overload:
        problems.append(f"the LCD shows {display_text!r}, not a number")
    if overload:
        display_text = ("-" if negative else "") + OVERLOAD_TEXT
    deciding = [
        item
        for region, item in zip(regions, scores, strict=True)
        if region.kind is not RegionKind.SYMBOL or region.symbol in DECIDING
    ]
    confidence = min((item.confidence for item in deciding), default=0.0)
    weak = [item.name for item in scores if item.confidence < score.MIN_CONFIDENCE]
    status = LocalStatus.READ
    if problems or confidence < score.MIN_CONFIDENCE:
        status = LocalStatus.UNCERTAIN
    reading = LocalReading(
        status=status,
        readable=True,
        display_text=display_text,
        digits=digits,
        digits_before_point=before,
        negative=negative,
        value=signed_value(display_text, digits, before) if digits is not None else None,
        unit=unit,
        mode=mode_of(symbols_on),
        flags=flags_of(symbols_on),
        overload=overload,
        confidence=round(confidence, 3),
        contrast=round(contrast, 3),
        weak_regions=weak,
        problems=problems,
    )
    return reading, chars


def decode_lcd(lcd: Gray, layout: LcdLayout) -> DecodedFrame:
    """Decode an LCD that is already warped to the layout size."""
    started = time.perf_counter()
    regions = layout_regions(layout)
    scores, contrast = score_regions(darkness_map(lcd), regions)
    if contrast < score.MIN_CONTRAST:
        reading, chars = unreadable(f"the LCD contrast {contrast:.2f} is below {score.MIN_CONTRAST}", contrast), []
    else:
        reading, chars = build_reading(regions, scores, layout, contrast)
    return DecodedFrame(
        reading=reading, chars=chars, regions=scores, elapsed=timedelta(seconds=time.perf_counter() - started)
    )


def frame_matches(image: NDArray[np.uint8], profile: MeterProfile) -> bool:
    """The frame has the size of the calibrated crop. Another size means that the webcam crop changed."""
    height, width = image.shape[:2]
    return (width, height) == (profile.image_width, profile.image_height)


def decode_with_lcd(image: NDArray[np.uint8], profile: MeterProfile) -> tuple[DecodedFrame, Gray | None]:
    """Decode a webcam crop (BGR or gray), and return the warped LCD too (None when the frame does not match)."""
    started = time.perf_counter()
    if not frame_matches(image, profile):
        height, width = image.shape[:2]
        problem = (
            f"the frame is {width}x{height}, the profile is for {profile.image_width}x{profile.image_height}: the "
            "webcam crop changed; calibrate again"
        )
        elapsed = timedelta(seconds=time.perf_counter() - started)
        return DecodedFrame(reading=unreadable(problem), chars=[], regions=[], elapsed=elapsed), None
    lcd = warp_lcd(to_gray(image), profile)
    decoded = decode_lcd(lcd, profile.layout)
    return decoded.model_copy(update={"elapsed": timedelta(seconds=time.perf_counter() - started)}), lcd


def decode_image(image: NDArray[np.uint8], profile: MeterProfile) -> DecodedFrame:
    """Decode a webcam crop (BGR or gray) with the profile."""
    return decode_with_lcd(image, profile)[0]


def fit_lcd(lcd: Gray, layout: LcdLayout) -> Gray:
    """A saved LCD image at the warp size of the layout (the aspect of a new profile can differ)."""
    size = warp_size(layout)
    return lcd if (lcd.shape[1], lcd.shape[0]) == size else cv2.resize(lcd, size, interpolation=cv2.INTER_LINEAR)


class ImageDecodeError(Exception):
    """The bytes are not an image that OpenCV can read."""


def read_image(data: bytes) -> NDArray[np.uint8]:
    image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ImageDecodeError("the frame is not a JPEG or PNG image")
    return image


def decode_jpeg(data: bytes, profile: MeterProfile) -> DecodedFrame:
    return decode_image(read_image(data), profile)


# endregion: frame
