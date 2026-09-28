"""The highlight layout (docs/overlay-layout.md, Geometry) and its shared test vectors.

The vectors are in docs/overlay-layout-vectors.json.

The app (Kotlin) passes the same vectors. Here: the Python reference gives the expected outputs, and the outputs
keep the rules of the spec (no overlap, inside the view, stable), so the vectors are right by the rules, not only
by this code.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from debug_devices_mcp.overlay_layout import CLEARANCE, COLOURS, LayoutInput, Rect, assign_tags, layout

ROOT = Path(__file__).resolve().parents[2]
VECTORS = json.loads((ROOT / "docs" / "overlay-layout-vectors.json").read_text())
CASES = VECTORS["cases"]
TOLERANCE = VECTORS["tolerance"]


def close(actual: object, expected: object) -> bool:
    """The same JSON value, with numbers equal within the tolerance of the vectors."""
    if isinstance(expected, dict) and isinstance(actual, dict):
        return actual.keys() == expected.keys() and all(close(actual[key], expected[key]) for key in expected)
    if isinstance(expected, list) and isinstance(actual, list):
        return len(actual) == len(expected) and all(close(a, e) for a, e in zip(actual, expected, strict=True))
    if isinstance(expected, float | int) and isinstance(actual, float | int) and not isinstance(expected, bool):
        return abs(actual - expected) <= TOLERANCE
    return actual == expected


def rect(data: dict) -> Rect:
    return Rect.model_validate(data)


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_the_reference_passes_the_vectors(case: dict) -> None:
    result = layout(LayoutInput.model_validate(case["input"])).model_dump(mode="json")
    assert close(result, case["expected"]), case["name"]


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_the_vectors_keep_the_rules(case: dict) -> None:
    data, result = case["input"], case["expected"]
    width, height = data["width"], data["height"]
    drawn = [rect(box["rect"]) for box in result["boxes"]]
    # Rule 4: every drawn box is at least min_box, around the centre of the real box.
    for real, box in zip(data["boxes"], drawn, strict=True):
        assert box.width >= data["min_box"] - TOLERANCE
        assert box.height >= data["min_box"] - TOLERANCE
        assert box.cx == pytest.approx(real["x"] + real["width"] / 2, abs=TOLERANCE)
    # Rule 1: the tags are unique; rule 5: the colours cycle in order.
    tags = [box["tag"] for box in result["boxes"]] + [arrow["tag"] for arrow in result["arrows"]]
    assert len(set(tags)) == len(tags)
    colours = [box["colour"] for box in result["boxes"]] + [arrow["colour"] for arrow in result["arrows"]]
    assert colours == [COLOURS[index % len(COLOURS)] for index in range(len(colours))]
    legend = result["legend"]
    if legend is not None:
        assert [row["tag"] for row in legend["rows"]] == tags
        if not legend["outside"]:
            assert not any(rect(legend["rect"]).overlaps(box.grown(CLEARANCE)) for box in drawn)
        else:
            assert result["extra_height"] > 0
    # Rule 3: a badge next to its box covers no box, no other badge, and not the legend; else it is outside.
    placed: list[Rect] = []
    for badge in result["badges"]:
        badge_rect = rect(badge["rect"])
        assert badge_rect.inside(width + TOLERANCE, height + TOLERANCE)
        if badge["outside"]:
            assert badge["leader"] is not None
        else:
            assert badge["leader"] is None
            assert not any(badge_rect.overlaps(box.grown(CLEARANCE)) for box in drawn)
            assert not any(badge_rect.overlaps(other.grown(CLEARANCE)) for other in placed)
            if legend is not None and not legend["outside"]:
                assert not badge_rect.overlaps(rect(legend["rect"]))
        placed.append(badge_rect)
    # Rule 7: the inset covers no box and not the legend.
    if result["inset"] is not None:
        dest = rect(result["inset"]["dest"])
        assert not any(dest.overlaps(box.grown(CLEARANCE)) for box in drawn)
        assert dest.width <= 0.25 * width + TOLERANCE


def test_the_cases_that_the_brief_asks_for() -> None:
    names = " | ".join(case["name"] for case in CASES)
    for words in ("two small pads", "cluster", "each edge", "every corner", "arrow", "fall back"):
        assert words in names
    by_name = {case["name"]: case["expected"] for case in CASES}
    corners = next(value for name, value in by_name.items() if "every corner" in name)
    assert corners["legend"]["outside"] is True
    fallback = next(value for name, value in by_name.items() if "fall back" in name)
    assert all(badge["outside"] for badge in fallback["badges"])


def test_stable_output() -> None:
    data = LayoutInput.model_validate(CASES[0]["input"])
    assert layout(data) == layout(data)


def test_tags() -> None:
    assert assign_tags([None, "A", None, "LONG", None]) == ["B", "A", "C", "LON", "D"]


def test_the_vectors_file_is_up_to_date() -> None:
    script = ROOT / "scripts" / "make_overlay_vectors.py"
    assert subprocess.run([sys.executable, str(script), "--check"], check=False).returncode == 0
