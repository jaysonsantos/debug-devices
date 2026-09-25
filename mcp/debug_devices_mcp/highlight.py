"""Highlight boxes: from pixels in the agent's last phone_snapshot to the phone overlay, and the annotated copy.

The agent gives boxes in pixels of the image that it got (after the server flips and the scaling). The phone wants
boxes on the true-orientation /v1/snapshot image, from 0 to 1 (docs/phone-api.md, POST /v1/overlay).
"""

import io
from typing import Annotated, ClassVar

from PIL import Image as PilImage
from PIL import ImageDraw, ImageFont
from pydantic import BaseModel, Field

from debug_devices_mcp.constants import images, phone
from debug_devices_mcp.focus import SnapshotGeometry
from debug_devices_mcp.phone_api import OverlayBox

# The same green as the phone overlay.
BOX_COLOR = (0, 230, 64)
BOX_WIDTH_PX = 3
LABEL_FONT_SIZE = 16
LABEL_PADDING_PX = 3
LABEL_BACKGROUND = (0, 0, 0)
LABEL_TEXT = (255, 255, 255)
ANNOTATED_QUALITY = 90
# Two sizes of the same photo: the width and height scales differ by at most this part (rounding).
SAME_SHAPE_TOLERANCE = 0.02


class PixelBox(BaseModel):
    """A box in pixels of the last phone_snapshot image that the agent got: top left corner, width, height."""

    x: float
    y: float
    width: Annotated[float, Field(gt=0)]
    height: Annotated[float, Field(gt=0)]
    label: Annotated[str, Field(max_length=phone.OVERLAY_MAX_LABEL)] = ""


class HighlightResult(BaseModel):
    ESTIMATE_NOTE: ClassVar[str] = (
        "The boxes are your estimate from the last phone_snapshot: say so to the user. Check them in the annotated "
        "image. Clear them (clear: true) when done or when the phone moves."
    )
    CLEARED_NOTE: ClassVar[str] = "The highlight boxes are removed."

    count: int
    # The boxes as the phone got them: on the true-orientation snapshot, from 0 to 1.
    boxes: list[OverlayBox]
    # The number of boxes that the phone shows now (from its status). None: an app without overlay_boxes.
    overlay_boxes: int | None
    note: str


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
    """Pixels in the flipped, scaled image -> a box from 0 to 1 on the true-orientation snapshot of the phone."""
    clipped = clip_box(box, geometry.width, geometry.height)
    unit_x, unit_y = clipped.x / geometry.width, clipped.y / geometry.height
    unit_width, unit_height = clipped.width / geometry.width, clipped.height / geometry.height
    # A flip mirrors the box: its far edge becomes its near edge.
    if geometry.orientation.flip_horizontal:
        unit_x = 1 - unit_x - unit_width
    if geometry.orientation.flip_vertical:
        unit_y = 1 - unit_y - unit_height
    return OverlayBox(
        snapshot_x=max(unit_x, 0.0),
        snapshot_y=max(unit_y, 0.0),
        width=unit_width,
        height=unit_height,
        label=clipped.label,
    )


def draw_boxes(jpeg: bytes, boxes: list[PixelBox]) -> bytes:
    """A copy of the image with green boxes and their labels (the boxes in pixels of this image)."""
    with PilImage.open(io.BytesIO(jpeg)) as opened:
        image = opened.convert(images.JPEG_MODE)
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=LABEL_FONT_SIZE)
    for box in boxes:
        clipped = clip_box(box, image.width, image.height)
        right, bottom = clipped.x + clipped.width, clipped.y + clipped.height
        draw.rectangle((clipped.x, clipped.y, right, bottom), outline=BOX_COLOR, width=BOX_WIDTH_PX)
        if not clipped.label:
            continue
        left, top, text_right, text_bottom = draw.textbbox((0, 0), clipped.label, font=font)
        text_height = text_bottom - top + 2 * LABEL_PADDING_PX
        # Above the box; below its top edge when there is no room above.
        label_y = clipped.y - text_height if clipped.y >= text_height else clipped.y + BOX_WIDTH_PX
        background = (clipped.x, label_y, clipped.x + text_right - left + 2 * LABEL_PADDING_PX, label_y + text_height)
        draw.rectangle(background, fill=LABEL_BACKGROUND)
        draw.text(
            (clipped.x + LABEL_PADDING_PX - left, label_y + LABEL_PADDING_PX - top),
            clipped.label,
            fill=LABEL_TEXT,
            font=font,
        )
    output = io.BytesIO()
    image.save(output, format=images.PIL_JPEG_FORMAT, quality=ANNOTATED_QUALITY)
    return output.getvalue()
