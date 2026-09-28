"""A crop of a phone_snapshot at full resolution: around a point or a box, enlarged, as an extra image.

The agent can check a probe tip or a small marking without its own crop step. The position is in pixels of the
image that the agent gets (after the turn, the flips, and the scaling); the crop comes from the full-resolution
still with the same turn and flips. No model judges the crop: the agent looks at it.
"""

import io
from typing import Annotated

from PIL import Image as PilImage
from pydantic import BaseModel, Field, model_validator

from debug_devices_mcp.constants import images

DEFAULT_RADIUS_PX = 60.0
DEFAULT_ZOOM = 2.0
MAX_ZOOM = 8.0
# The long side of the crop image at most (the zoom is reduced to fit).
MAX_CROP_SIDE = 1600
CROP_QUALITY = 92


class CropBox(BaseModel):
    x: float
    y: float
    width: Annotated[float, Field(gt=0)]
    height: Annotated[float, Field(gt=0)]


class SnapshotCrop(BaseModel):
    """Around (`x`, `y`) with `radius`, or the `box`; in pixels of the returned image. `zoom` enlarges the crop."""

    x: float | None = None
    y: float | None = None
    radius: Annotated[float, Field(gt=0)] = DEFAULT_RADIUS_PX
    box: CropBox | None = None
    zoom: Annotated[float, Field(ge=1, le=MAX_ZOOM)] = DEFAULT_ZOOM

    @model_validator(mode="after")
    def _one_area(self) -> SnapshotCrop:
        point = self.x is not None and self.y is not None
        if point == (self.box is not None) or (self.x is None) != (self.y is None):
            raise ValueError("give `x` and `y` (with `radius`), or `box`")
        return self

    def area(self) -> CropBox:
        if self.box is not None:
            return self.box
        x, y = self.x or 0.0, self.y or 0.0
        return CropBox(x=x - self.radius, y=y - self.radius, width=2 * self.radius, height=2 * self.radius)


class CropInfo(BaseModel):
    # The area in pixels of the returned image (cut at its edges), and in pixels of the full-resolution still.
    box_px: CropBox
    full_resolution_box_px: CropBox
    zoom: float
    width: int
    height: int


class CropOutsideError(ValueError):
    """The crop area is not on the image."""


def crop_snapshot(full_jpeg: bytes, shown_size: tuple[int, int], request: SnapshotCrop) -> tuple[bytes, CropInfo]:
    """Cut the area from the full-resolution still (already turned and flipped), enlarge it, and encode it."""
    with PilImage.open(io.BytesIO(full_jpeg)) as opened:
        image = opened.convert(images.JPEG_MODE)
    shown_width, shown_height = shown_size
    scale = image.width / shown_width
    area = request.area()
    left, top = max(area.x, 0.0), max(area.y, 0.0)
    right, bottom = min(area.x + area.width, float(shown_width)), min(area.y + area.height, float(shown_height))
    if right <= left or bottom <= top:
        raise CropOutsideError(f"the crop area is outside the image ({shown_width}x{shown_height} px)")
    box = (round(left * scale), round(top * scale), round(right * scale), round(bottom * scale))
    cropped = image.crop(box)
    # The zoom that keeps the crop image at most MAX_CROP_SIDE (below 1 for a very large area).
    zoom = min(request.zoom, MAX_CROP_SIDE / max(cropped.width, cropped.height))
    size = (max(1, round(cropped.width * zoom)), max(1, round(cropped.height * zoom)))
    enlarged = cropped.resize(size, PilImage.Resampling.LANCZOS) if size != cropped.size else cropped
    output = io.BytesIO()
    enlarged.save(output, format=images.PIL_JPEG_FORMAT, quality=CROP_QUALITY)
    info = CropInfo(
        box_px=CropBox(x=left, y=top, width=right - left, height=bottom - top),
        full_resolution_box_px=CropBox(x=box[0], y=box[1], width=box[2] - box[0], height=box[3] - box[1]),
        zoom=round(zoom, 3),
        width=enlarged.width,
        height=enlarged.height,
    )
    return output.getvalue(), info
