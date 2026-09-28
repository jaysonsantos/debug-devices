"""Highlight boxes: from pixels in the agent's last phone_snapshot to the phone overlay, and the annotated copy.

The agent gives boxes in pixels of the image that it got (after the server flips and the scaling). The phone wants
boxes on the true-orientation /v1/snapshot image, from 0 to 1 (docs/phone-api.md, POST /v1/overlay).
"""

import io
from enum import StrEnum
from typing import Annotated, ClassVar

from PIL import Image as PilImage
from pydantic import BaseModel, Field

from debug_devices_mcp.constants import phone
from debug_devices_mcp.focus import SnapshotGeometry
from debug_devices_mcp.overlay_draw import draw_layout
from debug_devices_mcp.overlay_layout import LayoutBox, assign_tags
from debug_devices_mcp.phone_api import OverlayBox, PreviewRegion

# The same green as the phone overlay.
BOX_COLOR = (0, 230, 64)
BOX_WIDTH_PX = 3
LABEL_FONT_SIZE = 16
LABEL_PADDING_PX = 3
LABEL_BACKGROUND = (0, 0, 0)
LABEL_TEXT = (255, 255, 255)
ANNOTATED_QUALITY = 90
# A box is fully on the phone screen when this part of its area is in the preview region (float rounding).
FULLY_INSIDE = 0.999
# Two sizes of the same photo: the width and height scales differ by at most this part (rounding).
SAME_SHAPE_TOLERANCE = 0.02


class PixelBox(BaseModel):
    """A box in pixels of the last phone_snapshot image that the agent got: top left corner, width, height."""

    x: float
    y: float
    width: Annotated[float, Field(gt=0)]
    height: Annotated[float, Field(gt=0)]
    label: Annotated[str, Field(max_length=phone.OVERLAY_MAX_LABEL)] = ""
    # A short tag (1-3 characters) at the box; without it, the next free letter (A, B, ...).
    tag: Annotated[str, Field(pattern=phone.OVERLAY_TAG_PATTERN)] | None = None


class LayoutSummary(BaseModel):
    """How the highlight layout came out (docs/overlay-layout.md)."""

    tags: list[str]
    # The legend with the labels went below the image: every corner had a box.
    legend_outside: bool
    # The tags whose badge had no free place next to the box (drawn outside the cluster, with a leader line).
    badges_outside: list[str]
    # An enlarged inset of small boxes is in the annotated image.
    inset: bool


class HighlightResult(BaseModel):
    ESTIMATE_NOTE: ClassVar[str] = (
        "The boxes are your estimate from the last phone_snapshot: say so to the user. Check them in the annotated "
        "image. Clear them (clear: true) when done. With live tracking they follow the board; after a move, take a "
        "fresh phone_snapshot before you say what is visible."
    )
    CLEARED_NOTE: ClassVar[str] = "The highlight boxes are removed."

    count: int
    # The boxes as the phone got them: on the true-orientation snapshot, from 0 to 1.
    boxes: list[OverlayBox]
    # The number of boxes that the phone shows now (from its status). None: an app without overlay_boxes.
    overlay_boxes: int | None
    note: str
    # Per box: is it on the phone screen (the preview can show only a part of the still)?
    visibility: list[BoxVisibility] = []
    warning: str | None = None
    # Set while the user hides the markings in the monitor.
    markings: str | None = None
    # The tags, and whether the legend or a badge had to move outside (the annotated image shows it).
    layout: LayoutSummary | None = None
    # "live tracking on: ..." (the boxes follow the board while the phone moves) or "no live tracking: <reason>".
    tracking: str | None = None


class InPreview(StrEnum):
    """Is a box on the phone screen? The app draws boxes only in a part of the still: `overlay_region` (the preview
    without the system bars and the status label), else `preview_region` for an older app."""

    FULLY = "fully"
    PARTLY = "partly"
    NOT = "not"
    # The app does not report its preview region.
    UNKNOWN = "unknown"


class BoxVisibility(BaseModel):
    label: str
    in_preview: InPreview


PREVIEW_WARNING = "not visible on the phone screen: move the phone or zoom out so it is near the centre"
PARTLY_WARNING = "only partly visible on the phone screen"


def in_preview(box: OverlayBox, region: PreviewRegion | None) -> InPreview:
    if region is None:
        return InPreview.UNKNOWN
    left = max(box.snapshot_x, region.snapshot_x)
    right = min(box.snapshot_x + box.width, region.snapshot_x + region.width)
    top = max(box.snapshot_y, region.snapshot_y)
    bottom = min(box.snapshot_y + box.height, region.snapshot_y + region.height)
    overlap = max(right - left, 0.0) * max(bottom - top, 0.0)
    area = box.width * box.height
    if overlap >= area * FULLY_INSIDE:
        return InPreview.FULLY
    return InPreview.PARTLY if overlap > 0 else InPreview.NOT


def visibility(boxes: list[OverlayBox], region: PreviewRegion | None) -> tuple[list[BoxVisibility], str | None]:
    """The visibility of each box, and a warning for the boxes that the user cannot see on the phone."""
    items = [BoxVisibility(label=box.label, in_preview=in_preview(box, region)) for box in boxes]
    hidden = [item.label or "a box" for item in items if item.in_preview is InPreview.NOT]
    partly = [item.label or "a box" for item in items if item.in_preview is InPreview.PARTLY]
    notes = []
    if hidden:
        notes.append(f"{', '.join(hidden)}: {PREVIEW_WARNING}")
    if partly:
        notes.append(f"{', '.join(partly)}: {PARTLY_WARNING}")
    return items, "; ".join(notes) or None


class BoxOutsideError(ValueError):
    """No part of the box is on the image."""


def clip_box(box: PixelBox, width: int, height: int) -> PixelBox:
    """The part of the box on the image. A box that sticks out is cut at the image edge."""
    left, top = max(box.x, 0.0), max(box.y, 0.0)
    right, bottom = min(box.x + box.width, float(width)), min(box.y + box.height, float(height))
    if right <= left or bottom <= top:
        raise BoxOutsideError(
            f"the box {box.label!r} at ({box.x:g}, {box.y:g}) is outside the image ({width}x{height} px)"
        )
    return box.model_copy(update={"x": left, "y": top, "width": right - left, "height": bottom - top})


def scale_boxes(boxes: list[PixelBox], source: tuple[int, int], target: tuple[int, int]) -> list[PixelBox]:
    """Boxes in pixels of a photo of size `source` -> pixels of the same photo at size `target`."""
    scale_x, scale_y = target[0] / source[0], target[1] / source[1]
    if abs(scale_x - scale_y) > SAME_SHAPE_TOLERANCE * max(scale_x, scale_y):
        raise BoxOutsideError(
            f"the photo ({source[0]}x{source[1]} px) has another shape than the last phone_snapshot "
            f"({target[0]}x{target[1]} px): register the last phone_snapshot"
        )
    return [
        box.model_copy(
            update={
                "x": box.x * scale_x,
                "y": box.y * scale_y,
                "width": box.width * scale_x,
                "height": box.height * scale_y,
            }
        )
        for box in boxes
    ]


def overlay_box(box: PixelBox, geometry: SnapshotGeometry) -> OverlayBox:
    """Pixels in the shown image (turned, flipped, scaled) -> a box from 0 to 1 on the still of the phone."""
    clipped = clip_box(box, geometry.width, geometry.height)
    unit_x, unit_y, unit_width, unit_height = geometry.orientation.box_to_true(
        clipped.x / geometry.width,
        clipped.y / geometry.height,
        clipped.width / geometry.width,
        clipped.height / geometry.height,
    )
    return OverlayBox(
        snapshot_x=max(unit_x, 0.0),
        snapshot_y=max(unit_y, 0.0),
        width=unit_width,
        height=unit_height,
        label=clipped.label,
        tag=clipped.tag,
    )


def with_tags(boxes: list[PixelBox]) -> list[PixelBox]:
    """Every box gets its tag (given ones stay; the others get the next free letter), so that the phone, the
    annotated image, and the page show the same tags."""
    tags = assign_tags([box.tag for box in boxes])
    return [box.model_copy(update={"tag": tag}) for box, tag in zip(boxes, tags, strict=True)]


def layout_boxes(boxes: list[PixelBox], width: int, height: int) -> list[LayoutBox]:
    boxes = [clip_box(box, width, height) for box in boxes]
    return [
        LayoutBox(x=box.x, y=box.y, width=box.width, height=box.height, label=box.label, tag=box.tag) for box in boxes
    ]


def draw_highlights(jpeg: bytes, boxes: list[PixelBox]) -> tuple[bytes, LayoutSummary]:
    """The annotated image by the highlight layout, and its summary."""
    with PilImage.open(io.BytesIO(jpeg)) as opened:
        width, height = opened.size
    annotated, result = draw_layout(jpeg, layout_boxes(boxes, width, height))
    summary = LayoutSummary(
        tags=[box.tag for box in result.boxes],
        legend_outside=result.legend is not None and result.legend.outside,
        badges_outside=[badge.tag for badge in result.badges if badge.outside],
        inset=result.inset is not None,
    )
    return annotated, summary


def draw_boxes(jpeg: bytes, boxes: list[PixelBox]) -> bytes:
    """A copy of the image with the boxes by the highlight layout (the boxes in pixels of this image)."""
    return draw_highlights(jpeg, boxes)[0]
