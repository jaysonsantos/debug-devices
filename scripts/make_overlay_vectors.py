#!/usr/bin/env python3
"""Write docs/overlay-layout-vectors.json: the shared test vectors of the highlight layout (docs/overlay-layout.md).

The inputs are the cases below; the expected outputs come from the Python reference layout
(mcp/debug_devices_mcp/overlay_layout.py). The phone app (Kotlin) must give the same outputs. Check a change with
the rendered pictures first (--render DIR), then write the file.

    uv run python scripts/make_overlay_vectors.py              # write the file
    uv run python scripts/make_overlay_vectors.py --check      # exit 1 when the file is not up to date
    uv run python scripts/make_overlay_vectors.py --render DIR # one PNG per case, to look at
"""

import argparse
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp"))

from debug_devices_mcp.overlay_layout import MIN_BOX_PHONE_DP, LayoutInput, layout

VECTORS = Path(__file__).resolve().parents[1] / "docs" / "overlay-layout-vectors.json"
SCHEMA_VERSION = 1
PHOTO = (1568, 1176)
PHONE = (411, 914)


def box(rect: tuple[float, float, float, float], label: str, tag: str | None = None) -> dict:
    """A box: `rect` is (x, y, width, height) in view units."""
    x, y, width, height = rect
    return {"x": x, "y": y, "width": width, "height": height, "label": label, **({"tag": tag} if tag else {})}


CASES: list[dict] = [
    {
        "name": "bench: two small pads 60 px apart (labels covered the other box)",
        "input": {
            "width": PHOTO[0],
            "height": PHOTO[1],
            "boxes": [box((700, 560, 14, 12), "C12 pad 1 (GND)"), box((760, 560, 14, 12), "C12 pad 2 (PP3V3)")],
        },
    },
    {
        "name": "four boxes in a cluster",
        "input": {
            "width": PHOTO[0],
            "height": PHOTO[1],
            "boxes": [
                box((760, 560, 40, 30), "U1"),
                box((806, 560, 40, 30), "U2"),
                box((760, 596, 40, 30), "U3"),
                box((806, 596, 40, 30), "U4"),
            ],
        },
    },
    {
        "name": "a box at each edge",
        "input": {
            "width": PHOTO[0],
            "height": PHOTO[1],
            "boxes": [
                box((700, 0, 60, 40), "top"),
                box((700, 1136, 60, 40), "bottom"),
                box((0, 560, 60, 40), "left"),
                box((1508, 560, 60, 40), "right"),
            ],
        },
    },
    {
        "name": "a box under every corner: the legend goes outside",
        "input": {
            "width": PHOTO[0],
            "height": PHOTO[1],
            "boxes": [
                box((10, 10, 120, 60), "J1"),
                box((1438, 10, 120, 60), "J2"),
                box((10, 1106, 120, 60), "J3"),
                box((1438, 1106, 120, 60), "J4"),
            ],
        },
    },
    {
        "name": "one box and an arrow",
        "input": {
            "width": PHOTO[0],
            "height": PHOTO[1],
            "boxes": [box((900, 400, 50, 50), "U7301")],
            "arrows": [{"angle_deg": 200, "label": "J4 ~4 cm"}],
        },
    },
    {
        "name": "arrows only",
        "input": {
            "width": PHOTO[0],
            "height": PHOTO[1],
            "arrows": [{"angle_deg": 0, "label": "TP9 ~2 cm"}, {"angle_deg": 270, "label": "U2 ~6 cm"}],
        },
    },
    {
        "name": "given tags and free letters",
        "input": {
            "width": PHOTO[0],
            "height": PHOTO[1],
            "boxes": [
                box((300, 300, 80, 60), "voltage regulator", "U1"),
                box((600, 300, 80, 60), "input cap"),
                box((900, 300, 80, 60), "output cap", "A"),
                box((1200, 300, 80, 60), "fuse"),
            ],
        },
    },
    {
        "name": "eight tiny boxes packed tight: the badges go around the cluster",
        "input": {
            "width": PHOTO[0],
            "height": PHOTO[1],
            "boxes": [
                box((740 + 12 * (index % 4), 560 + 12 * (index // 4), 8, 8), f"pin {index + 1}") for index in range(8)
            ],
        },
    },
    {
        "name": "a small view full of boxes: no free place, badges fall back with leader lines",
        "input": {
            "width": 200,
            "height": 150,
            "inset": False,
            "boxes": [
                box((10 + 62 * (index % 3), 8 + 70 * (index // 3), 56, 64), f"part {index + 1}") for index in range(6)
            ],
        },
    },
    {
        "name": "phone: two close boxes on a 411 x 914 dp screen (no inset)",
        "input": {
            "width": PHONE[0],
            "height": PHONE[1],
            "min_box": MIN_BOX_PHONE_DP,
            "inset": False,
            "boxes": [box((190, 440, 10, 8), "C12 pad 1"), box((214, 440, 10, 8), "C12 pad 2")],
        },
    },
]


def vectors() -> dict:
    cases = []
    for case in CASES:
        data = LayoutInput.model_validate(case["input"])
        expected = layout(data).model_dump(mode="json")
        cases.append({"name": case["name"], "input": data.model_dump(mode="json"), "expected": expected})
    return {
        "schema_version": SCHEMA_VERSION,
        "spec": "docs/overlay-layout.md (Geometry)",
        "tolerance": 0.01,
        "generated_by": "scripts/make_overlay_vectors.py (the Python reference layout)",
        "cases": cases,
    }


def render(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    font = ImageFont.load_default(size=13)
    for index, case in enumerate(vectors()["cases"]):
        data, result = case["input"], case["expected"]
        width, height = int(data["width"]), int(data["height"] + result["extra_height"])
        image = Image.new("RGB", (width, height), (150, 30, 30))
        draw = ImageDraw.Draw(image)
        for real in data["boxes"]:
            draw.rectangle(
                (real["x"], real["y"], real["x"] + real["width"], real["y"] + real["height"]), fill=(90, 90, 90)
            )
        for drawn in result["boxes"]:
            rect = drawn["rect"]
            draw.rectangle(
                (rect["x"], rect["y"], rect["x"] + rect["width"], rect["y"] + rect["height"]),
                outline=drawn["colour"],
                width=3,
            )
        for badge in result["badges"]:
            rect = badge["rect"]
            if badge["leader"]:
                draw.line(badge["leader"], fill=badge["colour"], width=1)
            draw.rectangle(
                (rect["x"], rect["y"], rect["x"] + rect["width"], rect["y"] + rect["height"]),
                fill=(0, 0, 0),
                outline=badge["colour"],
            )
            draw.text((rect["x"] + 4, rect["y"] + 2), badge["tag"], fill=badge["colour"], font=font)
        if result["legend"]:
            rect = result["legend"]["rect"]
            draw.rectangle(
                (rect["x"], rect["y"], rect["x"] + rect["width"], rect["y"] + rect["height"]), fill=(20, 20, 20)
            )
            for row_index, row in enumerate(result["legend"]["rows"]):
                draw.text(
                    (rect["x"] + 6, rect["y"] + 6 + 18 * row_index),
                    f"{row['tag']}: {row['label']}",
                    fill=row["colour"],
                    font=font,
                )
        for arrow in result["arrows"]:
            x, y = arrow["anchor"]
            draw.ellipse((x - 6, y - 6, x + 6, y + 6), outline=arrow["colour"], width=3)
        if result["inset"]:
            rect = result["inset"]["dest"]
            draw.rectangle(
                (rect["x"], rect["y"], rect["x"] + rect["width"], rect["y"] + rect["height"]),
                outline=(255, 255, 255),
                width=1,
            )
        image.save(directory / f"{index:02d}.png")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="exit 1 when the vectors file is not up to date")
    parser.add_argument("--render", type=Path, help="write one PNG per case to this folder")
    args = parser.parse_args()
    if args.render:
        render(args.render)
        return 0
    text = json.dumps(vectors(), indent=2) + "\n"
    if args.check:
        return 0 if VECTORS.exists() and VECTORS.read_text() == text else 1
    VECTORS.write_text(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
