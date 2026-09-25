"""Point the user to parts: a green box when a part is in the phone view, else an arrow toward it.

The map `board mm -> snapshot pixels` comes from the photo registration, and with live tracking from the tracker.
The snapshot pixels are those of the agent's snapshot (after the server flips). The arrows go to the phone with an
angle on the true-orientation snapshot image (docs/phone-api.md: 0 = right, 90 = down).
"""

import math

import numpy as np
from pydantic import BaseModel

from debug_devices_mcp.board.dump import Side
from debug_devices_mcp.board.model import Part
from debug_devices_mcp.constants import phone
from debug_devices_mcp.highlight import PixelBox
from debug_devices_mcp.images import SnapshotOrientation
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


def true_angle(angle_deg: float, orientation: SnapshotOrientation) -> float:
    """An angle in the flipped snapshot -> the same direction in the true-orientation snapshot."""
    if orientation.flip_horizontal:
        angle_deg = HALF_TURN - angle_deg
    if orientation.flip_vertical:
        angle_deg = -angle_deg
    return angle_deg % FULL_TURN


def shown_angle(angle_deg: float, orientation: SnapshotOrientation) -> float:
    """The inverse of `true_angle` (a flip is its own inverse)."""
    return true_angle(angle_deg, orientation)


def edge_point(center: np.ndarray, target: np.ndarray, width: float, height: float) -> np.ndarray:
    """Where the ray from the image center to the target leaves the image."""
    direction = target - center
    limits = []
    for axis, size in ((0, width), (1, height)):
        if direction[axis] > 0:
            limits.append((size - center[axis]) / direction[axis])
        elif direction[axis] < 0:
            limits.append(-center[axis] / direction[axis])
    return center + direction * min(limits)


def distance_label(distance_mm: float) -> str:
    cm = distance_mm / MM_PER_CM
    return f"~{round(cm)} cm" if cm >= WHOLE_CM_FROM else f"~{cm:.1f} cm"


def part_box(part: Part, matrix: Matrix) -> np.ndarray:
    """The part's boardview box corners in snapshot pixels (4, 2)."""
    box = part.box
    corners = np.array([(box.min_x, box.min_y), (box.max_x, box.min_y), (box.max_x, box.max_y), (box.min_x, box.max_y)])
    return apply(matrix, corners)


def plan(
    parts: list[Part],
    registered_side: Side,
    matrix: Matrix,
    size: tuple[int, int],
    orientation: SnapshotOrientation,
) -> PointPlan:
    width, height = size
    center = np.array([width / 2, height / 2])
    inverse = np.linalg.inv(matrix)
    boxes, arrows, targets = [], [], []
    for part in parts:
        if part.side not in {registered_side, Side.BOTH}:
            targets.append(TargetState(refdes=part.name, side=part.side, in_view=None, message=OTHER_SIDE_MESSAGE))
            continue
        target = apply(matrix, np.array([(part.center.x, part.center.y)]))[0]
        if 0 <= target[0] <= width and 0 <= target[1] <= height:
            corners = part_box(part, matrix)
            low, high = corners.min(axis=0), corners.max(axis=0)
            size_px = np.maximum(high - low, MIN_BOX_PX)
            if len(boxes) < phone.OVERLAY_MAX_BOXES:
                label = part.name[: phone.OVERLAY_MAX_LABEL]
                boxes.append(PixelBox(x=low[0], y=low[1], width=size_px[0], height=size_px[1], label=label))
            targets.append(TargetState(refdes=part.name, side=part.side, in_view=True, message=IN_VIEW_MESSAGE))
            continue
        edge = edge_point(center, target, width, height)
        edge_mm, target_mm = apply(inverse, np.array([edge, target]))
        distance_mm = float(np.linalg.norm(target_mm - edge_mm))
        angle = true_angle(math.degrees(math.atan2(target[1] - center[1], target[0] - center[0])), orientation)
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
