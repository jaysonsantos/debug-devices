import io

from PIL import Image

from debug_devices_mcp.images import downscale_jpeg

from .conftest import make_jpeg


def size_of(jpeg: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(jpeg)) as image:
        return image.size


def test_portrait_long_edge_is_limited() -> None:
    scaled = downscale_jpeg(make_jpeg(3060, 4080), 1568)

    assert (scaled.width, scaled.height) == (1176, 1568)
    assert size_of(scaled.data) == (1176, 1568)
    assert (scaled.original_width, scaled.original_height) == (3060, 4080)


def test_small_image_is_unchanged() -> None:
    jpeg = make_jpeg(640, 480)
    scaled = downscale_jpeg(jpeg, 1568)
    assert scaled.data == jpeg


def test_zero_keeps_full_size() -> None:
    jpeg = make_jpeg(2000, 1000)
    assert downscale_jpeg(jpeg, 0).data == jpeg


def low_quality_jpeg(width: int, height: int) -> bytes:
    output = io.BytesIO()
    noise = Image.effect_noise((width // 8, height // 8), 40).resize((width, height), Image.Resampling.BICUBIC)
    Image.merge("RGB", (noise, noise.rotate(180), noise)).save(output, format="JPEG", quality=60)
    return output.getvalue()


def test_scaled_image_is_not_larger_than_a_low_quality_source() -> None:
    source = low_quality_jpeg(1920, 1080)

    scaled = downscale_jpeg(source, 1568)

    assert (scaled.width, scaled.height) == (1568, 882)
    assert len(scaled.data) <= len(source)


def test_image_within_max_side_keeps_source_bytes() -> None:
    source = low_quality_jpeg(1568, 882)
    assert downscale_jpeg(source, 1568).data == source
