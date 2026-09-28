"""Point the user to parts: a green box when a part is in the phone view, else an arrow toward it.

The map `board mm -> snapshot pixels` comes from the photo registration, and with live tracking from the tracker.
The snapshot pixels are those of the agent's snapshot (after the server flips). The arrows go to the phone with an
angle on the true-orientation snapshot image (docs/phone-api.md: 0 = right, 90 = down).
"""

import math
from dataclasses import dataclass

import numpy as np
from pydantic import BaseModel

from debug_devices_mcp.board.dump import Side
from debug_devices_mcp.board.model import Part
from debug_devices_mcp.constants import phone
from debug_devices_mcp.highlight import BoxVisibility, PixelBox
from debug_devices_mcp.images import SnapshotOrientation
from debug_devices_mcp.orientation import as_transform
from debug_devices_mcp.phone_api import OverlayArrow, OverlayBox
from debug_devices_mcp.tracking import Matrix, apply

OTHER_SIDE_MESSAGE = "on the other side: isolate the power before you turn the board"
IN_VIEW_MESSAGE = "in view: green box"
OUT_OF_VIEW_MESSAGE = "outside the view: follow the arrow, {distance} away on the board"
# A box around a tiny part is at least this large (snapshot pixels), so it stays visible.
MIN_BOX_PX = 8.0
MM_PER_CM = 10
FULL_TURN = 360.0
HALF_TURN = 180.0
# Below 1 cm the label shows one decimal.
WHOLE_CM_FROM = 1.0


class TargetState(BaseModel):
    refdes: str
    side: Side
    # None: the part is on the other board side (no box, no arrow).
    in_view: bool | None
    # The arrow direction on the true-orientation snapshot image (0 = right, 90 = down), when out of view.
    angle_deg: float | None = None
    # The board distance from the view edge to the part, when out of view.
    distance_cm: float | None = None
    message: str


class PointPlan(BaseModel):
    """What to show for the targets, in pixels of the current snapshot (boxes) and true angles (arrows)."""

    boxes: list[PixelBox]
    arrows: list[OverlayArrow]
    targets: list[TargetState]


class PointResult(BaseModel):
    count: int
    boxes: list[OverlayBox]
    arrows: list[OverlayArrow]
    targets: list[TargetState]
    # True when the live tracker follows the board: the boxes and arrows move with the phone.
    tracking: bool
    overlay_boxes: int | None
    overlay_arrows: int | None
    note: str
    # Per box: is it on the phone screen? And a warning for the boxes that the user cannot see there.
    visibility: list[BoxVisibility] = []
    warning: str | None = None
    # Set while the user hides the markings in the monitor.
    markings: str | None = None


def true_angle(angle_deg: float, orientation: SnapshotOrientation) -> float:
    """An angle in the shown snapshot (turned, flipped) -> the same direction in the still of the phone."""
    return as_transform(orientation).angle_to_true(angle_deg)


def shown_angle(angle_deg: float, orientation: SnapshotOrientation) -> float:
    """The inverse of `true_angle`."""
    return as_transform(orientation).angle_from_true(angle_deg)


def edge_point(center: np.ndarray, target: np.ndarray, low: np.ndarray, high: np.ndarray) -> np.ndarray:
    """Where the ray from the view center to the target leaves the view (the rectangle low..high)."""
    direction = target - center
    limits = []
    for axis in (0, 1):
        if direction[axis] > 0:
            limits.append((high[axis] - center[axis]) / direction[axis])
        elif direction[axis] < 0:
            limits.append((low[axis] - center[axis]) / direction[axis])
    return center + direction * min(limits)


def distance_label(distance_mm: float) -> str:
    cm = distance_mm / MM_PER_CM
    return f"~{round(cm)} cm" if cm >= WHOLE_CM_FROM else f"~{cm:.1f} cm"


def part_box(part: Part, matrix: Matrix) -> np.ndarray:
    """The part's boardview box corners in snapshot pixels (4, 2)."""
    box = part.box
    corners = np.array([(box.min_x, box.min_y), (box.max_x, box.min_y), (box.max_x, box.max_y), (box.min_x, box.max_y)])
    return apply(matrix, corners)


@dataclass(frozen=True)
class ImageFrame:
    """The agent's image: its size and transform, and `view`: the part that the phone screen shows (x, y, width,
    height in pixels). Without `view` (the app does not report it), the whole image."""

    size: tuple[int, int]
    orientation: SnapshotOrientation
    view: tuple[float, float, float, float] | None = None


def view_bounds(frame: ImageFrame) -> tuple[np.ndarray, np.ndarray]:
    """The low and high corners of the view in pixels of the agent's image."""
    width, height = frame.size
    x0, y0, view_width, view_height = frame.view if frame.view is not None else (0.0, 0.0, float(width), float(height))
    return np.array([x0, y0]), np.array([x0 + view_width, y0 + view_height])


def in_view(point: np.ndarray, low: np.ndarray, high: np.ndarray) -> bool:
    return bool(low[0] <= point[0] <= high[0] and low[1] <= point[1] <= high[1])


def arrow_angle(center: np.ndarray, target: np.ndarray, orientation: SnapshotOrientation) -> float:
    """The direction from the view center to the target, on the true-orientation still."""
    return true_angle(math.degrees(math.atan2(target[1] - center[1], target[0] - center[0])), orientation)


def corners_of(box: PixelBox) -> np.ndarray:
    right, bottom = box.x + box.width, box.y + box.height
    return np.array([(box.x, box.y), (right, box.y), (right, bottom), (box.x, bottom)])


def follow_boxes(boxes: list[PixelBox], matrix: Matrix, frame: ImageFrame) -> tuple[list[PixelBox], list[OverlayArrow]]:
    """Plain highlight boxes (pixels of the snapshot at phone_highlight) through `matrix` to a snapshot taken now.
    A box whose center is in the view: the bounding box of its moved corners, with its label and tag. Else an
    arrow from the view center toward it, with its label (or tag)."""
    low, high = view_bounds(frame)
    center = (low + high) / 2
    shown, arrows = [], []
    for box in boxes:
        corners = apply(matrix, corners_of(box))
        middle = apply(matrix, np.array([(box.x + box.width / 2, box.y + box.height / 2)]))[0]
        if in_view(middle, low, high):
            box_low = corners.min(axis=0)
            size_px = np.maximum(corners.max(axis=0) - box_low, MIN_BOX_PX)
            update = {"x": float(box_low[0]), "y": float(box_low[1]), "width": float(size_px[0])}
            shown.append(box.model_copy(update={**update, "height": float(size_px[1])}))
        elif len(arrows) < phone.OVERLAY_MAX_ARROWS:
            angle = arrow_angle(center, middle, frame.orientation)
            label = (box.label or box.tag or "")[: phone.OVERLAY_MAX_LABEL]
            # The arrow keeps the box's tag (C3): the phone and the page give it the same tag and colour.
            arrows.append(OverlayArrow(angle_deg=round(angle, 1), label=label, tag=box.tag))
    return shown, arrows


def plan(parts: list[Part], registered_side: Side, matrix: Matrix, frame: ImageFrame) -> PointPlan:
    """A part is in view when its center is in the view; else an arrow from the view center, at the view edge."""
    orientation = frame.orientation
    low, high = view_bounds(frame)
    center = (low + high) / 2
    inverse = np.linalg.inv(matrix)
    boxes, arrows, targets = [], [], []
    for part in parts:
        if part.side not in {registered_side, Side.BOTH}:
            targets.append(TargetState(refdes=part.name, side=part.side, in_view=None, message=OTHER_SIDE_MESSAGE))
            continue
        target = apply(matrix, np.array([(part.center.x, part.center.y)]))[0]
        if in_view(target, low, high):
            corners = part_box(part, matrix)
            box_low, box_high = corners.min(axis=0), corners.max(axis=0)
            size_px = np.maximum(box_high - box_low, MIN_BOX_PX)
            if len(boxes) < phone.OVERLAY_MAX_BOXES:
                label = part.name[: phone.OVERLAY_MAX_LABEL]
                boxes.append(PixelBox(x=box_low[0], y=box_low[1], width=size_px[0], height=size_px[1], label=label))
            targets.append(TargetState(refdes=part.name, side=part.side, in_view=True, message=IN_VIEW_MESSAGE))
            continue
        edge = edge_point(center, target, low, high)
        edge_mm, target_mm = apply(inverse, np.array([edge, target]))
        distance_mm = float(np.linalg.norm(target_mm - edge_mm))
        angle = arrow_angle(center, target, orientation)
        label = f"{part.name} {distance_label(distance_mm)}"[: phone.OVERLAY_MAX_LABEL]
        if len(arrows) < phone.OVERLAY_MAX_ARROWS:
            arrows.append(OverlayArrow(angle_deg=round(angle, 1), label=label))
        targets.append(
            TargetState(
                refdes=part.name,
                side=part.side,
                in_view=False,
                angle_deg=round(angle, 1),
                distance_cm=round(distance_mm / MM_PER_CM, 1),
                message=OUT_OF_VIEW_MESSAGE.format(distance=distance_label(distance_mm)),
            )
        )
    return PointPlan(boxes=boxes, arrows=arrows, targets=targets)
