"""Calibration from one saved webcam crop: find the LCD, fit the template, and draw the regions for a check.

The LCD is the largest bright quadrilateral that does not touch the crop border (a bright wall behind the meter
touches it). The fit moves and scales the digit row of the template a little, to make the segments most decisive.
"""

from datetime import UTC, datetime

import cv2
import numpy as np
from numpy.typing import NDArray

from debug_devices_mcp.sevenseg.constants import TEMPLATE_T21D, calibration, warp
from debug_devices_mcp.sevenseg.decode import (
    BLANK,
    GRAY_DIMENSIONS,
    UINT8_MAX,
    DecodedFrame,
    Gray,
    RegionKind,
    darkness_map,
    decode_digit,
    decode_image,
    layout_regions,
    odd,
    score_regions,
    segment_name,
    to_gray,
    warp_lcd,
)
from debug_devices_mcp.sevenseg.profile import LcdLayout, MeterProfile, Point, Rect, Segment
from debug_devices_mcp.sevenseg.template import TEMPLATES

type Color = NDArray[np.uint8]

# Brightness levels (percentiles of the crop) to try when the Otsu split does not find the LCD.
FALLBACK_PERCENTILES = (50, 60, 70, 80)
# A quadrilateral candidate fills at least this part of its rotated bounding box.
MIN_RECTANGULARITY = 0.8
BLANK_FIT_WEIGHT = 0.5
HEADER_HEIGHT = 36
TEXT_SCALE = 0.6
TEXT_COLOR = (255, 255, 255)
HEADER_COLOR = (40, 40, 40)
TEXT_ORIGIN = (8, 24)


class CalibrationError(Exception):
    """The LCD was not found in the image."""


# region: LCD corners


def order_corners(points: NDArray[np.float32]) -> list[Point]:
    """Top left, top right, bottom right, bottom left."""
    sums = points.sum(axis=1)
    differences = np.diff(points, axis=1).ravel()
    ordered = (
        points[np.argmin(sums)],
        points[np.argmin(differences)],
        points[np.argmax(sums)],
        points[np.argmax(differences)],
    )
    return [Point(x=float(x), y=float(y)) for x, y in ordered]


def quadrilateral(contour: NDArray[np.int32]) -> NDArray[np.float32]:
    hull = cv2.convexHull(contour)
    approx = cv2.approxPolyDP(hull, calibration.POLY_EPSILON_FRACTION * cv2.arcLength(hull, True), True)
    if len(approx) == calibration.QUAD_CORNERS:
        return approx.reshape(-1, 2).astype(np.float32)
    return cv2.boxPoints(cv2.minAreaRect(contour)).astype(np.float32)


def lcd_candidates(bright: Gray) -> list[tuple[float, NDArray[np.float32]]]:
    height, width = bright.shape
    contours, _ = cv2.findContours(bright, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    candidates = []
    for contour in contours:
        x, y, box_width, box_height = cv2.boundingRect(contour)
        if x <= 0 or y <= 0 or x + box_width >= width or y + box_height >= height:
            continue
        area = cv2.contourArea(contour)
        if area < calibration.MIN_LCD_AREA_FRACTION * width * height:
            continue
        (_, _), (rect_width, rect_height), _ = cv2.minAreaRect(contour)
        if area < MIN_RECTANGULARITY * rect_width * rect_height:
            continue
        candidates.append((area, quadrilateral(contour)))
    return candidates


def find_lcd_corners(image: NDArray[np.uint8]) -> list[Point]:
    gray = cv2.GaussianBlur(to_gray(image), (5, 5), 0)
    kernel_size = odd(min(gray.shape) * calibration.CLOSE_KERNEL_FRACTION)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (kernel_size, kernel_size))
    otsu, _ = cv2.threshold(gray, 0, UINT8_MAX, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    levels = [otsu, *(float(np.percentile(gray, percentile)) for percentile in FALLBACK_PERCENTILES)]
    for level in levels:
        _, bright = cv2.threshold(gray, level, UINT8_MAX, cv2.THRESH_BINARY)
        candidates = lcd_candidates(cv2.morphologyEx(bright, cv2.MORPH_CLOSE, kernel))
        if candidates:
            return order_corners(max(candidates, key=lambda candidate: candidate[0])[1])
    raise CalibrationError(
        "no bright quadrilateral (the LCD) inside the crop: turn on the backlight, or set the crop around the meter"
    )


def measured_aspect(corners: list[Point]) -> float:
    top_left, top_right, bottom_right, bottom_left = ((point.x, point.y) for point in corners)
    width = (np.hypot(*np.subtract(top_right, top_left)) + np.hypot(*np.subtract(bottom_right, bottom_left))) / 2
    height = (np.hypot(*np.subtract(bottom_left, top_left)) + np.hypot(*np.subtract(bottom_right, top_right))) / 2
    return float(np.clip(width / max(height, 1.0), warp.MIN_ASPECT, warp.MAX_ASPECT))


# endregion: LCD corners

# region: fit


def moved_digit_row(layout: LcdLayout, dx: float, dy: float, factor: float) -> LcdLayout:
    """The digit boxes, the points, and the sign moved by (dx, dy) and scaled around the center of the digit row."""
    left = min(box.x for box in layout.digits)
    right = max(box.x + box.width for box in layout.digits)
    top = min(box.y for box in layout.digits)
    bottom = max(box.y + box.height for box in layout.digits)
    center = Point(x=(left + right) / 2, y=(top + bottom) / 2)

    def change(rect: Rect) -> Rect:
        return rect.scaled(factor, center).moved(dx, dy)

    return layout.model_copy(
        update={
            "digits": [change(box) for box in layout.digits],
            "points": [change(point) for point in layout.points],
            "sign": change(layout.sign),
        }
    )


def fit_score(layout: LcdLayout, darkness: NDArray[np.float32]) -> float:
    """Known digit patterns (a blank counts half), plus the mean segment confidence."""
    regions = [region for region in layout_regions(layout) if region.kind is RegionKind.SEGMENT]
    scores, _ = score_regions(darkness, regions)
    by_name = {item.name: item for item in scores}
    total = float(np.mean([item.confidence for item in scores]))
    for index in range(len(layout.digits)):
        on = frozenset(segment for segment in Segment if by_name[segment_name(index, segment)].on)
        char, exact = decode_digit(on)
        if exact:
            total += BLANK_FIT_WEIGHT if char == BLANK else 1.0
    return total


def best_fit(
    layout: LcdLayout, darkness: NDArray[np.float32], shifts: tuple[float, ...], scales: tuple[float, ...]
) -> tuple[float, float, float]:
    """The (dx, dy, scale) with the best fit score; a smaller move wins a tie."""
    candidates = [
        (fit_score(moved_digit_row(layout, dx, dy, factor), darkness), -abs(dx) - abs(dy), (dx, dy, factor))
        for factor in scales
        for dx in shifts
        for dy in shifts
    ]
    return max(candidates, key=lambda candidate: candidate[:2])[2]


def fit_layout(lcd: Gray, layout: LcdLayout) -> LcdLayout:
    """A coarse search, then a fine search around its best move."""
    darkness = darkness_map(lcd)
    dx, dy, factor = best_fit(layout, darkness, calibration.FIT_SHIFTS, calibration.FIT_SCALES)
    coarse = moved_digit_row(layout, dx, dy, factor)
    fine_dx, fine_dy, fine_factor = best_fit(coarse, darkness, calibration.FINE_SHIFTS, calibration.FINE_SCALES)
    return moved_digit_row(coarse, fine_dx, fine_dy, fine_factor)


# endregion: fit


def calibrate(
    image: NDArray[np.uint8], template: str = TEMPLATE_T21D, fit: bool = True
) -> tuple[MeterProfile, DecodedFrame]:
    """A profile for this crop, and the decoded calibration frame (to check it)."""
    if template not in TEMPLATES:
        raise CalibrationError(f"unknown template {template!r}; known: {', '.join(TEMPLATES)}")
    corners = find_lcd_corners(image)
    layout = TEMPLATES[template]().model_copy(update={"aspect": measured_aspect(corners)})
    height, width = image.shape[:2]
    profile = MeterProfile(
        template=template,
        image_width=width,
        image_height=height,
        corners=corners,
        layout=layout,
        calibrated_at=datetime.now(UTC),
    )
    if fit:
        fitted = fit_layout(warp_lcd(to_gray(image), profile), layout)
        profile = profile.model_copy(update={"layout": fitted})
    return profile, decode_image(image, profile)


# region: annotation


def to_color(image: NDArray[np.uint8]) -> Color:
    return cv2.cvtColor(image, cv2.COLOR_GRAY2BGR) if image.ndim == GRAY_DIMENSIONS else image.copy()


def annotate(image: NDArray[np.uint8], profile: MeterProfile, decoded: DecodedFrame) -> Color:
    """The warped LCD with every region (green: on, red: off), the reading above it, and the crop with the corners."""
    scale = calibration.ANNOTATION_SCALE
    lcd = to_color(warp_lcd(to_gray(image), profile))
    lcd = cv2.resize(lcd, (lcd.shape[1] * scale, lcd.shape[0] * scale), interpolation=cv2.INTER_NEAREST)
    height, width = lcd.shape[:2]
    on = {item.name: item.on for item in decoded.regions}
    for region in layout_regions(profile.layout):
        points = np.array([(x * width, y * height) for x, y in region.polygon]).round().astype(np.int32)
        color = calibration.ON_COLOR if on.get(region.name) else calibration.OFF_COLOR
        cv2.polylines(lcd, [points], True, color, calibration.LINE_THICKNESS)
    reading = decoded.reading
    header = np.full((HEADER_HEIGHT, width, 3), HEADER_COLOR, dtype=np.uint8)
    text = f"{reading.display_text or '-'} {reading.unit} {reading.mode} ({reading.status}, conf {reading.confidence})"
    cv2.putText(header, text, TEXT_ORIGIN, cv2.FONT_HERSHEY_SIMPLEX, TEXT_SCALE, TEXT_COLOR, 1, cv2.LINE_AA)
    crop = to_color(image)
    corners = np.array([(point.x, point.y) for point in profile.corners]).round().astype(np.int32)
    cv2.polylines(crop, [corners], True, calibration.LCD_COLOR, max(1, round(min(crop.shape[:2]) / 200)))
    crop = cv2.resize(crop, (width, round(crop.shape[0] * width / crop.shape[1])), interpolation=cv2.INTER_AREA)
    return np.vstack([header, lcd, crop])


# endregion: annotation
