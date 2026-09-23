"""Scale JPEG images down before they go to the model."""

import io

from PIL import Image as PilImage
from PIL import ImageOps
from pydantic import BaseModel

from debug_devices_mcp.constants import images


class ScaledJpeg(BaseModel):
    data: bytes
    width: int
    height: int
    original_width: int
    original_height: int


def downscale_jpeg(jpeg: bytes, max_side: int) -> ScaledJpeg:
    """Scale so that the long edge is at most `max_side` pixels. `max_side` 0 keeps the full size.

    The EXIF orientation is applied first, so the result is upright without EXIF data.
    """
    with PilImage.open(io.BytesIO(jpeg)) as opened:
        original_width, original_height = opened.size
        if not max_side or max(opened.size) <= max_side:
            return ScaledJpeg(
                data=jpeg,
                width=original_width,
                height=original_height,
                original_width=original_width,
                original_height=original_height,
            )
        upright = ImageOps.exif_transpose(opened)
        upright.thumbnail((max_side, max_side), PilImage.Resampling.LANCZOS)
        rgb = upright.convert(images.JPEG_MODE)
        data = b""
        # A source with a low JPEG quality can be smaller than our re-encode. Lower the quality until the
        # scaled image is not larger than the source, or the lowest step is reached.
        for quality in images.JPEG_QUALITY_STEPS:
            output = io.BytesIO()
            rgb.save(output, format=images.PIL_JPEG_FORMAT, quality=quality, optimize=True)
            data = output.getvalue()
            if len(data) <= len(jpeg):
                break
        return ScaledJpeg(
            data=data,
            width=upright.width,
            height=upright.height,
            original_width=original_width,
            original_height=original_height,
        )
