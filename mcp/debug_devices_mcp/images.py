"""Scale JPEG images down before they go to the model."""

import io

from PIL import Image as PilImage
from PIL import ImageOps
from pydantic import BaseModel, ConfigDict

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


# region: orientation


class SnapshotOrientation(BaseModel):
    """Flips of the phone snapshot that the user chose. The tools and the monitor page show the photo this way."""

    # A value: hashable, so the page render cache can use it as a key.
    model_config = ConfigDict(frozen=True)

    flip_horizontal: bool = False
    flip_vertical: bool = False

    @property
    def unchanged(self) -> bool:
        return not (self.flip_horizontal or self.flip_vertical)

    def describe(self) -> str:
        if self.flip_horizontal and self.flip_vertical:
            return "flipped horizontally and vertically"
        if self.flip_horizontal:
            return "flipped horizontally"
        if self.flip_vertical:
            return "flipped vertically"
        return "as taken (no flip)"


def orient_jpeg(jpeg: bytes, orientation: SnapshotOrientation) -> bytes:
    """Apply the EXIF rotation, then the flips. With no flip, the source bytes come back unchanged."""
    return transform_jpeg(jpeg, 0, orientation.flip_horizontal, orientation.flip_vertical)


FULL_TURN = 360
# PIL turns counterclockwise: these give a clockwise turn of the key in degrees.
CLOCKWISE = {90: PilImage.Transpose.ROTATE_270, 180: PilImage.Transpose.ROTATE_180, 270: PilImage.Transpose.ROTATE_90}


# The EXIF orientation values that need no turn (no tag, or "normal").
NO_EXIF_TURN = frozenset({None, 1})
EXIF_ORIENTATION_TAG = 0x0112


def exif_orientation(jpeg: bytes) -> int | None:
    with PilImage.open(io.BytesIO(jpeg)) as opened:
        return opened.getexif().get(EXIF_ORIENTATION_TAG)


def transform_jpeg(jpeg: bytes, turn_degrees: int, flip_horizontal: bool, flip_vertical: bool) -> bytes:
    """Apply the EXIF rotation, then a clockwise quarter turn, then the flips (orientation.ImageTransform).

    With no turn, no flip, and no EXIF rotation, the source bytes come back unchanged. An EXIF rotation is always
    applied, so the width and height of the result are the ones that a viewer shows. The result has no EXIF
    orientation. It is not larger than the source, like `downscale_jpeg`.
    """
    turn = turn_degrees % FULL_TURN
    if not (turn or flip_horizontal or flip_vertical) and exif_orientation(jpeg) in NO_EXIF_TURN:
        return jpeg
    with PilImage.open(io.BytesIO(jpeg)) as opened:
        image = ImageOps.exif_transpose(opened)
        if turn:
            image = image.transpose(CLOCKWISE[turn])
        if flip_horizontal:
            image = ImageOps.mirror(image)
        if flip_vertical:
            image = ImageOps.flip(image)
        rgb = image.convert(images.JPEG_MODE)
    data = b""
    for quality in images.JPEG_QUALITY_STEPS:
        output = io.BytesIO()
        rgb.save(output, format=images.PIL_JPEG_FORMAT, quality=quality, optimize=True)
        data = output.getvalue()
        if len(data) <= len(jpeg):
            break
    return data


# endregion: orientation
