#!/usr/bin/env python3
"""Contract checks for the phone camera HTTP API in docs/phone-api.md.

Run it against any base URL:

    python3 scripts/qa_contract.py --base-url http://127.0.0.1:8765
    adb -s <serial> forward tcp:8765 tcp:8765 && python3 scripts/qa_contract.py

The script uses the standard library only. It changes the zoom and the torch of the camera,
and it puts back the zoom ratio and the torch state that it found at the start.
Exit code 0 means every check passed.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import struct
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

# region: constants

ZOOM_STEP_FACTOR = 1.5
ZOOM_TOLERANCE = 1e-3
MAX_STEPS_TO_REACH_MAX_ZOOM = 64
START_TIMEOUT_SECONDS = 30.0
START_POLL_SECONDS = 0.05
STABLE_SECONDS = 3.0
RACE_RATIO = 3.0
CONCURRENT_ZOOM_RATIOS = (1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0)

DEFAULT_PORT = 8765
DEFAULT_BASE_URL = f"http://127.0.0.1:{DEFAULT_PORT}"
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_SNAPSHOT_TIMEOUT_SECONDS = 30.0


class Env(StrEnum):
    BASE_URL = "QA_PHONE_BASE_URL"
    TIMEOUT = "QA_PHONE_TIMEOUT_SECONDS"
    SNAPSHOT_TIMEOUT = "QA_PHONE_SNAPSHOT_TIMEOUT_SECONDS"


class Route(StrEnum):
    HEALTH = "/v1/health"
    STATUS = "/v1/status"
    ZOOM = "/v1/zoom"
    TORCH = "/v1/torch"
    ROTATION = "/v1/rotation"
    PREVIEW = "/v1/preview"
    SNAPSHOT = "/v1/snapshot"
    UNKNOWN = "/v1/does-not-exist"


class Method(StrEnum):
    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    DELETE = "DELETE"


class ContentType(StrEnum):
    JSON = "application/json"
    JPEG = "image/jpeg"
    TEXT = "text/plain"


class ErrorCode(StrEnum):
    CAMERA_NOT_READY = "camera_not_ready"
    NO_FLASH_UNIT = "no_flash_unit"
    BAD_REQUEST = "bad_request"
    NOT_FOUND = "not_found"
    METHOD_NOT_ALLOWED = "method_not_allowed"
    CAPTURE_FAILED = "capture_failed"
    INTERNAL_ERROR = "internal_error"


ERROR_STATUS: dict[ErrorCode, int] = {
    ErrorCode.CAMERA_NOT_READY: 503,
    ErrorCode.NO_FLASH_UNIT: 409,
    ErrorCode.BAD_REQUEST: 400,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.METHOD_NOT_ALLOWED: 405,
    ErrorCode.CAPTURE_FAILED: 500,
    ErrorCode.INTERNAL_ERROR: 500,
}

HTTP_OK = 200

CAMERA_STATUS_FIELDS: dict[str, type] = {
    "zoom_ratio": float,
    "min_zoom_ratio": float,
    "max_zoom_ratio": float,
    "torch_enabled": bool,
    "has_flash_unit": bool,
    "rotation_degrees": int,
    "rotation_locked": bool,
    "preview_flip_horizontal": bool,
    "preview_flip_vertical": bool,
}
PREVIEW_FLIPS = ((True, False), (False, True), (True, True), (False, False))
# Objects in CameraStatus, checked by expect_focus and expect_optics.
CAMERA_STATUS_OBJECTS = ("focus", "optics")
FOCUS_FIELDS = ("distance_diopters", "state", "calibration", "min_distance_diopters")
FOCUS_STATES = ("focused", "scanning", "unfocused", "unknown")
FOCUS_CALIBRATIONS = ("uncalibrated", "approximate", "calibrated")
OPTICS_FIELDS = ("focal_length_mm", "sensor_width_mm", "output_width_px")
ROTATIONS = (0, 90, 180, 270)
QUARTER_TURN = 90
HALF_TURN = 180
API_ERROR_FIELDS = ("error", "message")

JPEG_SOI = b"\xff\xd8"
JPEG_EOI = b"\xff\xd9"
JPEG_MARKER_PREFIX = 0xFF
JPEG_SEGMENT_HEADER_BYTES = 4
# SOF0..SOF15 carry the frame size, except DHT (C4), JPG (C8) and DAC (CC).
JPEG_SOF_MARKERS = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}


class Expect(StrEnum):
    """What the camera state is on the device under test."""

    READY = "ready"
    NOT_READY = "not-ready"
    # The app is in the background (HOME pressed). Health 200, camera endpoints 503 camera_not_ready.
    BACKGROUND = "background"
    # Run right after `am start`. Status polls see only 503 camera_not_ready, then the start state.
    STARTING = "starting"
    # Run right after `am start`. Zoom requests during the start must not crash the app (bug 1 of round 1).
    STARTING_RACE = "starting-race"


# endregion: constants

# region: http


@dataclass(frozen=True)
class Response:
    status: int
    content_type: str
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body)


@dataclass(frozen=True)
class CameraStatus:
    zoom_ratio: float
    min_zoom_ratio: float
    max_zoom_ratio: float
    torch_enabled: bool
    has_flash_unit: bool
    rotation_degrees: int
    rotation_locked: bool
    preview_flip_horizontal: bool
    preview_flip_vertical: bool


class ContractError(AssertionError):
    """One contract check failed."""


class Client:
    def __init__(self, base_url: str, timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def request(
        self,
        method: Method,
        path: str,
        body: bytes | None = None,
        timeout: float | None = None,
        content_type: str | None = ContentType.JSON,
    ) -> Response:
        headers = {"Content-Type": content_type} if body is not None and content_type else {}
        req = urllib.request.Request(self.base_url + path, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                return Response(resp.status, resp.headers.get("Content-Type", ""), resp.read())
        except urllib.error.HTTPError as err:
            return Response(err.code, err.headers.get("Content-Type", ""), err.read())

    def get(self, path: str, timeout: float | None = None) -> Response:
        return self.request(Method.GET, path, timeout=timeout)

    def post_json(self, path: str, payload: Any) -> Response:
        return self.request(Method.POST, path, json.dumps(payload).encode())

    def post_raw(self, path: str, body: bytes) -> Response:
        return self.request(Method.POST, path, body)


# endregion: http

# region: assertions


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise ContractError(message)


def expect_json(resp: Response) -> Any:
    expect(
        resp.content_type.startswith(ContentType.JSON),
        f"content type is {resp.content_type!r}, expected {ContentType.JSON}",
    )
    try:
        return resp.json()
    except json.JSONDecodeError as err:
        raise ContractError(f"body is not JSON: {err}: {resp.body[:200]!r}") from err


def is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def expect_focus(focus: object) -> None:
    """`focus` is null (camera not bound yet) or an object with the four fields."""
    if focus is None:
        return
    expect(isinstance(focus, dict), f"focus is not an object or null: {focus!r}")
    assert isinstance(focus, dict)
    expect(set(focus) == set(FOCUS_FIELDS), f"focus keys {sorted(focus)} != {sorted(FOCUS_FIELDS)}")
    diopters = focus["distance_diopters"]
    expect(diopters is None or (is_number(diopters) and diopters >= 0), f"focus.distance_diopters {diopters!r}")
    expect(focus["state"] in FOCUS_STATES, f"focus.state {focus['state']!r} is not in {FOCUS_STATES}")
    expect(focus["calibration"] in FOCUS_CALIBRATIONS, f"focus.calibration {focus['calibration']!r}")
    minimum = focus["min_distance_diopters"]
    expect(is_number(minimum) and minimum >= 0, f"focus.min_distance_diopters {minimum!r}")


def expect_optics(optics: object) -> None:
    expect(isinstance(optics, dict), f"optics is not an object: {optics!r}")
    assert isinstance(optics, dict)
    expect(set(optics) == set(OPTICS_FIELDS), f"optics keys {sorted(optics)} != {sorted(OPTICS_FIELDS)}")
    for name in ("focal_length_mm", "sensor_width_mm"):
        expect(is_number(optics[name]) and optics[name] > 0, f"optics.{name} {optics[name]!r} is not > 0")
    width = optics["output_width_px"]
    expect(isinstance(width, int) and not isinstance(width, bool) and width > 0, f"optics.output_width_px {width!r}")


def expect_status(resp: Response) -> CameraStatus:
    expect(resp.status == HTTP_OK, f"HTTP {resp.status}, expected {HTTP_OK}: {resp.body[:200]!r}")
    data = expect_json(resp)
    expect(isinstance(data, dict), f"CameraStatus is not an object: {data!r}")
    expected_keys = set(CAMERA_STATUS_FIELDS) | set(CAMERA_STATUS_OBJECTS)
    expect(set(data) == expected_keys, f"CameraStatus keys {sorted(data)} != {sorted(expected_keys)}")
    expect_focus(data["focus"])
    expect_optics(data["optics"])
    for name, kind in CAMERA_STATUS_FIELDS.items():
        value = data[name]
        if kind is float:
            ok = isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)
        elif kind is int:
            ok = isinstance(value, int) and not isinstance(value, bool)
        else:
            ok = isinstance(value, bool)
        expect(ok, f"CameraStatus.{name} = {value!r} is not a {kind.__name__}")
    status = CameraStatus(**{name: kind(data[name]) for name, kind in CAMERA_STATUS_FIELDS.items()})
    expect(status.min_zoom_ratio > 0, f"min_zoom_ratio {status.min_zoom_ratio} is not > 0")
    expect(status.min_zoom_ratio <= status.max_zoom_ratio, f"min {status.min_zoom_ratio} > max {status.max_zoom_ratio}")
    expect(
        status.min_zoom_ratio - ZOOM_TOLERANCE <= status.zoom_ratio <= status.max_zoom_ratio + ZOOM_TOLERANCE,
        f"zoom_ratio {status.zoom_ratio} is outside [{status.min_zoom_ratio}, {status.max_zoom_ratio}]",
    )
    expect(status.rotation_degrees in ROTATIONS, f"rotation_degrees {status.rotation_degrees} is not in {ROTATIONS}")
    return status


def expect_error(resp: Response, code: ErrorCode) -> None:
    expected_status = ERROR_STATUS[code]
    expect(resp.status == expected_status, f"HTTP {resp.status}, expected {expected_status} ({code})")
    data = expect_json(resp)
    expect(isinstance(data, dict), f"ApiError is not an object: {data!r}")
    expect(set(data) == set(API_ERROR_FIELDS), f"ApiError keys {sorted(data)} != {sorted(API_ERROR_FIELDS)}")
    expect(data["error"] == code, f"error {data['error']!r}, expected {code!r}")
    expect(isinstance(data["message"], str) and data["message"], f"message {data['message']!r} is empty")


def expect_zoom(actual: float, wanted: float, what: str) -> None:
    expect(math.isclose(actual, wanted, abs_tol=ZOOM_TOLERANCE), f"{what}: zoom_ratio {actual}, expected {wanted}")


def jpeg_size(data: bytes) -> tuple[int, int]:
    """Return (width, height) from the first SOF segment of a JPEG."""
    index = len(JPEG_SOI)
    while index + JPEG_SEGMENT_HEADER_BYTES <= len(data):
        expect(data[index] == JPEG_MARKER_PREFIX, f"JPEG marker expected at byte {index}")
        marker = data[index + 1]
        length = struct.unpack(">H", data[index + 2 : index + 4])[0]
        if marker in JPEG_SOF_MARKERS:
            height, width = struct.unpack(">HH", data[index + 5 : index + 9])
            return width, height
        index += 2 + length
    raise ContractError("JPEG has no SOF segment")


# EXIF orientation values that turn the image by a quarter turn (width and height swap).
EXIF_QUARTER_TURN_ORIENTATIONS = frozenset({5, 6, 7, 8})
EXIF_ORIENTATION_TO_DEGREES = {1: 0, 3: 180, 6: 90, 8: 270}
EXIF_APP1 = 0xE1
EXIF_HEADER = b"Exif\x00\x00"
EXIF_ORIENTATION_TAG = 0x0112
TIFF_LITTLE_ENDIAN = b"II"
TIFF_IFD_ENTRY_BYTES = 12
JPEG_SOS = 0xDA


def jpeg_exif_orientation(data: bytes) -> int | None:
    """Return the EXIF orientation (1-8) of a JPEG, or None when it has no orientation tag."""
    index = len(JPEG_SOI)
    while index + JPEG_SEGMENT_HEADER_BYTES <= len(data) and data[index] == JPEG_MARKER_PREFIX:
        marker = data[index + 1]
        length = struct.unpack(">H", data[index + 2 : index + 4])[0]
        if marker == JPEG_SOS:
            return None
        segment = data[index + 4 : index + 2 + length]
        if marker == EXIF_APP1 and segment.startswith(EXIF_HEADER):
            tiff = segment[len(EXIF_HEADER) :]
            order = "<" if tiff[:2] == TIFF_LITTLE_ENDIAN else ">"
            ifd = struct.unpack(order + "I", tiff[4:8])[0]
            count = struct.unpack(order + "H", tiff[ifd : ifd + 2])[0]
            for entry in range(count):
                start = ifd + 2 + entry * TIFF_IFD_ENTRY_BYTES
                tag, _, _ = struct.unpack(order + "HHI", tiff[start : start + 8])
                if tag == EXIF_ORIENTATION_TAG:
                    return struct.unpack(order + "H", tiff[start + 8 : start + 10])[0]
            return None
        index += 2 + length
    return None


def displayed_size(data: bytes) -> tuple[int, int]:
    """Width and height as a viewer shows the JPEG: the pixel size, turned by the EXIF orientation."""
    width, height = jpeg_size(data)
    if jpeg_exif_orientation(data) in EXIF_QUARTER_TURN_ORIENTATIONS:
        return height, width
    return width, height


# endregion: assertions

# region: checks


@dataclass
class Context:
    client: Client
    snapshot_timeout: float
    strict: bool
    notes: list[str] = field(default_factory=list)

    def status(self) -> CameraStatus:
        return expect_status(self.client.get(Route.STATUS))

    def zoom_ratio(self, ratio: float) -> CameraStatus:
        return expect_status(self.client.post_json(Route.ZOOM, {"ratio": ratio}))

    def zoom_step(self, step: str) -> CameraStatus:
        return expect_status(self.client.post_json(Route.ZOOM, {"step": step}))


def check_health(ctx: Context) -> None:
    resp = ctx.client.get(Route.HEALTH)
    expect(resp.status == HTTP_OK, f"HTTP {resp.status}")
    data = expect_json(resp)
    expect(data.get("ok") is True, f"ok is {data.get('ok')!r}")
    expect(isinstance(data.get("app_version"), str) and data["app_version"], "app_version is missing")
    ctx.notes.append(f"app_version={data['app_version']}")


def check_status(ctx: Context) -> None:
    status = ctx.status()
    ctx.notes.append(f"zoom=[{status.min_zoom_ratio}, {status.max_zoom_ratio}] flash={status.has_flash_unit}")


def check_zoom_ratio(ctx: Context) -> None:
    first = ctx.status()
    low, high = first.min_zoom_ratio, first.max_zoom_ratio
    expect_zoom(ctx.zoom_ratio(low).zoom_ratio, low, "ratio=min")
    middle = (low + high) / 2
    expect_zoom(ctx.zoom_ratio(middle).zoom_ratio, middle, "ratio=middle")
    expect_zoom(ctx.status().zoom_ratio, middle, "GET status after ratio=middle")
    expect_zoom(ctx.zoom_ratio(high).zoom_ratio, high, "ratio=max")


def check_zoom_clamp(ctx: Context) -> None:
    first = ctx.status()
    low, high = first.min_zoom_ratio, first.max_zoom_ratio
    expect_zoom(ctx.zoom_ratio(high * 10).zoom_ratio, high, "ratio above max is clamped")
    expect_zoom(ctx.zoom_ratio(low / 10).zoom_ratio, low, "ratio below min is clamped")
    expect_zoom(ctx.zoom_ratio(0).zoom_ratio, low, "ratio 0 is clamped")
    expect_zoom(ctx.zoom_ratio(-3).zoom_ratio, low, "negative ratio is clamped")


def check_zoom_step(ctx: Context) -> None:
    low = ctx.zoom_ratio(ctx.status().min_zoom_ratio)
    wanted = min(low.min_zoom_ratio * ZOOM_STEP_FACTOR, low.max_zoom_ratio)
    after_in = ctx.zoom_step("in")
    expect_zoom(after_in.zoom_ratio, wanted, "step in from min")
    expect_zoom(ctx.zoom_step("out").zoom_ratio, low.min_zoom_ratio, "step out back to min")
    expect_zoom(ctx.zoom_step("out").zoom_ratio, low.min_zoom_ratio, "step out at min stays at min")

    middle = ctx.zoom_ratio(low.min_zoom_ratio * ZOOM_STEP_FACTOR)
    expect_zoom(
        ctx.zoom_step("out").zoom_ratio,
        max(middle.zoom_ratio / ZOOM_STEP_FACTOR, low.min_zoom_ratio),
        "step out divides by the factor",
    )


def check_zoom_step_to_max(ctx: Context) -> None:
    status = ctx.zoom_ratio(ctx.status().min_zoom_ratio)
    wanted = status.zoom_ratio
    for _ in range(MAX_STEPS_TO_REACH_MAX_ZOOM):
        wanted = min(wanted * ZOOM_STEP_FACTOR, status.max_zoom_ratio)
        after = ctx.zoom_step("in")
        expect_zoom(after.zoom_ratio, wanted, "step in")
        if math.isclose(after.zoom_ratio, status.max_zoom_ratio, abs_tol=ZOOM_TOLERANCE):
            break
    else:
        raise ContractError(f"max zoom not reached after {MAX_STEPS_TO_REACH_MAX_ZOOM} steps")
    expect_zoom(ctx.zoom_step("in").zoom_ratio, status.max_zoom_ratio, "step in at max stays at max")


BAD_ZOOM_BODIES: list[tuple[str, bytes]] = [
    ("empty body", b""),
    ("not JSON", b"zoom please"),
    ("JSON array", b"[1.5]"),
    ("empty object", b"{}"),
    ("unknown step", b'{"step": "sideways"}'),
    ("step is not a string", b'{"step": 1}'),
    ("ratio is a string", b'{"ratio": "2"}'),
    ("ratio is a bool", b'{"ratio": true}'),
    ("ratio is null", b'{"ratio": null}'),
    ("both ratio and step", b'{"ratio": 2, "step": "in"}'),
]
# Cases that the contract does not state. Only --strict checks them.
STRICT_BAD_ZOOM_BODIES: list[tuple[str, bytes]] = [
    ("ratio overflows a double", b'{"ratio": 1e400}'),
    ("step with wrong case", b'{"step": "IN"}'),
]


def check_zoom_bad_request(ctx: Context) -> None:
    before = ctx.status()
    bodies = BAD_ZOOM_BODIES + (STRICT_BAD_ZOOM_BODIES if ctx.strict else [])
    for name, body in bodies:
        try:
            expect_error(ctx.client.post_raw(Route.ZOOM, body), ErrorCode.BAD_REQUEST)
        except ContractError as err:
            raise ContractError(f"zoom {name}: {err}") from err
    expect_zoom(ctx.status().zoom_ratio, before.zoom_ratio, "zoom is unchanged after bad requests")


def check_torch(ctx: Context) -> None:
    status = ctx.status()
    if not status.has_flash_unit:
        expect_error(ctx.client.post_json(Route.TORCH, {"enabled": True}), ErrorCode.NO_FLASH_UNIT)
        expect(ctx.status().torch_enabled is False, "torch_enabled is true without a flash unit")
        ctx.notes.append("no flash unit: checked 409 only")
        return
    on = expect_status(ctx.client.post_json(Route.TORCH, {"enabled": True}))
    expect(on.torch_enabled is True, "torch on: torch_enabled is not true")
    expect(ctx.status().torch_enabled is True, "GET status after torch on: torch_enabled is not true")
    off = expect_status(ctx.client.post_json(Route.TORCH, {"enabled": False}))
    expect(off.torch_enabled is False, "torch off: torch_enabled is not false")


BAD_TORCH_BODIES: list[tuple[str, bytes]] = [
    ("empty body", b""),
    ("not JSON", b"on"),
    ("empty object", b"{}"),
    ("enabled is a string", b'{"enabled": "yes"}'),
    ("enabled is a number", b'{"enabled": 1}'),
]


def check_torch_bad_request(ctx: Context) -> None:
    for name, body in BAD_TORCH_BODIES:
        try:
            expect_error(ctx.client.post_raw(Route.TORCH, body), ErrorCode.BAD_REQUEST)
        except ContractError as err:
            raise ContractError(f"torch {name}: {err}") from err


def check_snapshot(ctx: Context) -> None:
    resp = ctx.client.get(Route.SNAPSHOT, timeout=ctx.snapshot_timeout)
    expect(resp.status == HTTP_OK, f"HTTP {resp.status}: {resp.body[:200]!r}")
    expect(resp.content_type.startswith(ContentType.JPEG), f"content type is {resp.content_type!r}")
    expect(resp.body.startswith(JPEG_SOI), "body does not start with the JPEG SOI marker")
    expect(resp.body.rstrip(b"\x00").endswith(JPEG_EOI), "body does not end with the JPEG EOI marker")
    width, height = jpeg_size(resp.body)
    ctx.notes.append(f"snapshot {width}x{height} {len(resp.body)} bytes")


def check_snapshot_keeps_torch(ctx: Context) -> None:
    """The snapshot does not fire the flash, and the torch state stays the same."""
    if not ctx.status().has_flash_unit:
        ctx.notes.append("no flash unit: skipped")
        return
    for enabled in (True, False):
        expect_status(ctx.client.post_json(Route.TORCH, {"enabled": enabled}))
        resp = ctx.client.get(Route.SNAPSHOT, timeout=ctx.snapshot_timeout)
        expect(resp.status == HTTP_OK, f"snapshot with torch={enabled}: HTTP {resp.status}")
        after = ctx.status().torch_enabled
        expect(after is enabled, f"torch_enabled is {after} after a snapshot with torch={enabled}")


# Known paths and the methods that they do not accept.
WRONG_METHODS: list[tuple[Method, Route]] = [
    (Method.GET, Route.ZOOM),
    (Method.GET, Route.TORCH),
    (Method.GET, Route.ROTATION),
    (Method.GET, Route.PREVIEW),
    (Method.POST, Route.STATUS),
    (Method.POST, Route.HEALTH),
    (Method.POST, Route.SNAPSHOT),
]
STRICT_WRONG_METHODS: list[tuple[Method, Route]] = [
    (Method.PUT, Route.ZOOM),
    (Method.DELETE, Route.STATUS),
]


def check_method_not_allowed(ctx: Context) -> None:
    for method, route in WRONG_METHODS + (STRICT_WRONG_METHODS if ctx.strict else []):
        body = b"{}" if method in (Method.POST, Method.PUT) else None
        try:
            expect_error(ctx.client.request(method, route, body), ErrorCode.METHOD_NOT_ALLOWED)
        except ContractError as err:
            raise ContractError(f"{method} {route}: {err}") from err


def expect_start_state(status: CameraStatus, what: str) -> None:
    expect(status.torch_enabled is False, f"{what}: torch is on")
    expect_zoom(status.zoom_ratio, status.min_zoom_ratio, what)
    expect(status.rotation_locked is False, f"{what}: rotation is locked, expected auto")
    expect(not status.preview_flip_horizontal and not status.preview_flip_vertical, f"{what}: the preview is flipped")


def check_after_start(ctx: Context) -> None:
    """Right after an app start, the torch is off, the zoom is at min, and the rotation is auto."""
    expect_start_state(ctx.status(), "after the app start")


def check_rotation(ctx: Context) -> None:
    for degrees in ROTATIONS:
        locked = expect_status(ctx.client.post_json(Route.ROTATION, {"degrees": degrees}))
        expect(locked.rotation_degrees == degrees, f"lock {degrees}: rotation_degrees {locked.rotation_degrees}")
        expect(locked.rotation_locked is True, f"lock {degrees}: rotation_locked is not true")
        again = ctx.status()
        expect(again.rotation_degrees == degrees and again.rotation_locked, f"GET status after lock {degrees}: {again}")
    auto = expect_status(ctx.client.post_json(Route.ROTATION, {"auto": True}))
    expect(auto.rotation_locked is False, "auto: rotation_locked is not false")
    ctx.notes.append(f"auto gives {auto.rotation_degrees} degrees")


BAD_PREVIEW_BODIES: list[tuple[str, bytes]] = [
    ("empty body", b"{}"),
    ("flip_vertical missing", b'{"flip_horizontal": true}'),
    ("flip_horizontal missing", b'{"flip_vertical": false}'),
    ("flip_horizontal is a string", b'{"flip_horizontal": "yes", "flip_vertical": false}'),
    ("flip_vertical is a number", b'{"flip_horizontal": false, "flip_vertical": 1}'),
    ("flip_horizontal is null", b'{"flip_horizontal": null, "flip_vertical": false}'),
    ("unknown field", b'{"flip_horizontal": true, "flip_vertical": false, "rotate": 90}'),
]


def check_preview(ctx: Context) -> None:
    """The preview flips follow each POST, the status shows them, and bad bodies are refused with no change."""
    for flip_horizontal, flip_vertical in PREVIEW_FLIPS:
        body = {"flip_horizontal": flip_horizontal, "flip_vertical": flip_vertical}
        status = expect_status(ctx.client.post_json(Route.PREVIEW, body))
        again = ctx.status()
        for what, got in (("POST", status), ("GET status", again)):
            expect(
                (got.preview_flip_horizontal, got.preview_flip_vertical) == (flip_horizontal, flip_vertical),
                f"preview {body}: {what} shows {got.preview_flip_horizontal}, {got.preview_flip_vertical}",
            )
    for name, raw in BAD_PREVIEW_BODIES:
        try:
            expect_error(ctx.client.post_raw(Route.PREVIEW, raw), ErrorCode.BAD_REQUEST)
        except ContractError as err:
            raise ContractError(f"preview {name}: {err}") from err
    after = ctx.status()
    expect(
        not after.preview_flip_horizontal and not after.preview_flip_vertical,
        "the preview flips changed after bad requests",
    )


def snapshot_bytes(ctx: Context) -> bytes:
    resp = ctx.client.get(Route.SNAPSHOT, timeout=ctx.snapshot_timeout)
    expect(resp.status == HTTP_OK, f"snapshot: HTTP {resp.status}")
    return resp.body


def check_snapshot_rotation(ctx: Context) -> None:
    """A locked rotation turns the next snapshot. 90 and 270 swap width and height compared with 0 and 180."""
    sizes: dict[int, tuple[int, int]] = {}
    orientations: dict[int, int | None] = {}
    for degrees in ROTATIONS:
        expect_status(ctx.client.post_json(Route.ROTATION, {"degrees": degrees}))
        data = snapshot_bytes(ctx)
        sizes[degrees] = displayed_size(data)
        orientations[degrees] = jpeg_exif_orientation(data)
    expect_status(ctx.client.post_json(Route.ROTATION, {"auto": True}))
    upright, turned = sizes[0], sizes[QUARTER_TURN]
    expect(turned == (upright[1], upright[0]), f"rotation 90 shows {turned}, rotation 0 shows {upright}: not swapped")
    expect(sizes[HALF_TURN] == upright, f"rotation 180 shows {sizes[HALF_TURN]}, rotation 0 shows {upright}")
    expect(sizes[270] == turned, f"rotation 270 shows {sizes[270]}, rotation 90 shows {turned}")
    ctx.notes.append(f"displayed sizes {sizes}, EXIF orientation {orientations}")


BAD_ROTATION_BODIES: list[tuple[str, bytes]] = [
    ("empty body", b""),
    ("not JSON", b"90"),
    ("empty object", b"{}"),
    ("degrees 45", b'{"degrees": 45}'),
    ("degrees is a string", b'{"degrees": "90"}'),
    ("degrees is a bool", b'{"degrees": true}'),
    ("degrees 90.5", b'{"degrees": 90.5}'),
    ("auto false", b'{"auto": false}'),
    ("auto is a string", b'{"auto": "true"}'),
    ("both degrees and auto", b'{"degrees": 90, "auto": true}'),
]
STRICT_BAD_ROTATION_BODIES: list[tuple[str, bytes]] = [
    ("degrees -90", b'{"degrees": -90}'),
    ("degrees 360", b'{"degrees": 360}'),
    ("degrees is null", b'{"degrees": null}'),
    ("auto is null", b'{"auto": null}'),
]


def check_rotation_bad_request(ctx: Context) -> None:
    before = ctx.status()
    for name, body in BAD_ROTATION_BODIES + (STRICT_BAD_ROTATION_BODIES if ctx.strict else []):
        try:
            expect_error(ctx.client.post_raw(Route.ROTATION, body), ErrorCode.BAD_REQUEST)
        except ContractError as err:
            raise ContractError(f"rotation {name}: {err}") from err
    after = ctx.status()
    expect(after.rotation_locked == before.rotation_locked, "rotation_locked changed after bad requests")


def check_post_needs_json_content_type(ctx: Context) -> None:
    """A POST body without `Content-Type: application/json` gives 400, and nothing changes."""
    before = ctx.status()
    cases = [
        (Route.ZOOM, b'{"step": "in"}'),
        (Route.TORCH, b'{"enabled": false}'),
        (Route.ROTATION, b'{"degrees": 90}'),
    ]
    for route, body in cases:
        for content_type in (None, ContentType.TEXT):
            resp = ctx.client.request(Method.POST, route, body, content_type=content_type)
            try:
                expect_error(resp, ErrorCode.BAD_REQUEST)
            except ContractError as err:
                raise ContractError(f"{route} with Content-Type {content_type}: {err}") from err
    after = ctx.status()
    expect_zoom(after.zoom_ratio, before.zoom_ratio, "zoom after requests without the JSON content type")
    expect(after.rotation_locked == before.rotation_locked, "rotation changed after a request without JSON type")


def check_concurrent_zoom(ctx: Context) -> None:
    """Zoom changes run one at a time. A request never cancels another one, so every request gets 200."""
    status = ctx.status()
    ratios = [min(max(r, status.min_zoom_ratio), status.max_zoom_ratio) for r in CONCURRENT_ZOOM_RATIOS]
    with ThreadPoolExecutor(max_workers=len(ratios)) as pool:
        responses = list(pool.map(lambda r: ctx.client.post_json(Route.ZOOM, {"ratio": r}), ratios))
    for ratio, resp in zip(ratios, responses, strict=True):
        try:
            expect_zoom(expect_status(resp).zoom_ratio, ratio, f"concurrent ratio {ratio}")
        except ContractError as err:
            raise ContractError(f"concurrent zoom {ratio}: {err}") from err
    final = ctx.status().zoom_ratio
    expect(any(abs(final - r) <= ZOOM_TOLERANCE for r in ratios), f"final zoom {final} is none of {ratios}")


def wait_for_server(ctx: Context, deadline: float) -> None:
    while True:
        try:
            ctx.client.get(Route.HEALTH)
        except OSError:
            expect(time.monotonic() < deadline, "the HTTP server did not start")
            time.sleep(START_POLL_SECONDS)
        else:
            return


def expect_alive(ctx: Context, seconds: float) -> None:
    """Poll health and status for `seconds`. A connection error means that the app crashed."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            ctx.client.get(Route.HEALTH)
            ctx.client.get(Route.STATUS)
        except OSError as err:
            raise ContractError(f"the app stopped answering (crash?): {err}") from err
        time.sleep(START_POLL_SECONDS)


def check_start_sequence(ctx: Context) -> None:
    """After `am start`: health 200, status 503 camera_not_ready, then 200 with the start state."""
    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    wait_for_server(ctx, deadline)
    not_ready = 0
    while True:
        resp = ctx.client.get(Route.STATUS)
        if resp.status == HTTP_OK:
            break
        expect_error(resp, ErrorCode.CAMERA_NOT_READY)
        not_ready += 1
        expect(time.monotonic() < deadline, f"status still 503 after {START_TIMEOUT_SECONDS} s")
        time.sleep(START_POLL_SECONDS)
    expect_start_state(expect_status(resp), "first 200 status")
    ctx.notes.append(f"{not_ready} x 503 before the first 200")


def check_start_race(ctx: Context) -> None:
    """After `am start`: send zoom requests until one gets 200. The app must answer and stay up."""
    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    wait_for_server(ctx, deadline)
    not_ready = 0
    while True:
        try:
            resp = ctx.client.post_json(Route.ZOOM, {"ratio": RACE_RATIO})
        except OSError as err:
            raise ContractError(f"zoom during the start: the app stopped answering (crash?): {err}") from err
        if resp.status == HTTP_OK:
            break
        expect_error(resp, ErrorCode.CAMERA_NOT_READY)
        not_ready += 1
        expect(time.monotonic() < deadline, f"zoom still 503 after {START_TIMEOUT_SECONDS} s")
        time.sleep(START_POLL_SECONDS)
    status = expect_status(resp)
    wanted = min(max(RACE_RATIO, status.min_zoom_ratio), status.max_zoom_ratio)
    expect_zoom(status.zoom_ratio, wanted, "first 200 zoom")
    expect_alive(ctx, STABLE_SECONDS)
    expect_zoom(ctx.status().zoom_ratio, wanted, f"zoom {STABLE_SECONDS} s after the first 200")
    ctx.notes.append(f"{not_ready} x 503 before the first 200, alive for {STABLE_SECONDS} s")


def check_internal_error(ctx: Context) -> None:
    expect_error(ctx.client.get(Route.STATUS), ErrorCode.INTERNAL_ERROR)


def check_not_found(ctx: Context) -> None:
    expect_error(ctx.client.get(Route.UNKNOWN), ErrorCode.NOT_FOUND)
    expect_error(ctx.client.post_json(Route.UNKNOWN, {}), ErrorCode.NOT_FOUND)


def check_not_ready(ctx: Context) -> None:
    check_health(ctx)
    expect_error(ctx.client.get(Route.STATUS), ErrorCode.CAMERA_NOT_READY)
    expect_error(ctx.client.post_json(Route.ZOOM, {"step": "in"}), ErrorCode.CAMERA_NOT_READY)
    expect_error(ctx.client.post_json(Route.TORCH, {"enabled": False}), ErrorCode.CAMERA_NOT_READY)
    expect_error(ctx.client.post_json(Route.ROTATION, {"auto": True}), ErrorCode.CAMERA_NOT_READY)
    expect_error(ctx.client.get(Route.SNAPSHOT, timeout=ctx.snapshot_timeout), ErrorCode.CAMERA_NOT_READY)


def check_capture_failed(ctx: Context) -> None:
    expect_error(ctx.client.get(Route.SNAPSHOT, timeout=ctx.snapshot_timeout), ErrorCode.CAPTURE_FAILED)


Check = Callable[[Context], None]

READY_CHECKS: list[Check] = [
    check_health,
    check_status,
    check_zoom_ratio,
    check_zoom_clamp,
    check_zoom_step,
    check_zoom_step_to_max,
    check_zoom_bad_request,
    check_concurrent_zoom,
    check_torch,
    check_torch_bad_request,
    check_rotation,
    check_preview,
    check_rotation_bad_request,
    check_post_needs_json_content_type,
    check_snapshot,
    check_snapshot_keeps_torch,
    check_snapshot_rotation,
    check_method_not_allowed,
    check_not_found,
]
NOT_READY_CHECKS: list[Check] = [check_not_ready, check_method_not_allowed, check_not_found]

# endregion: checks

# region: runner


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str


def restore(ctx: Context, initial: CameraStatus) -> None:
    try:
        ctx.client.post_json(Route.ZOOM, {"ratio": initial.zoom_ratio})
        if initial.has_flash_unit:
            ctx.client.post_json(Route.TORCH, {"enabled": initial.torch_enabled})
        rotation = {"degrees": initial.rotation_degrees} if initial.rotation_locked else {"auto": True}
        ctx.client.post_json(Route.ROTATION, rotation)
    except OSError as err:
        print(f"could not restore the camera state: {err}", file=sys.stderr)


def run_checks(ctx: Context, checks: list[Check]) -> list[CheckResult]:
    results: list[CheckResult] = []
    for check in checks:
        ctx.notes.clear()
        name = check.__name__.removeprefix("check_")
        try:
            check(ctx)
        except ContractError as err:
            results.append(CheckResult(name, False, str(err)))
        except (OSError, ValueError) as err:
            results.append(CheckResult(name, False, f"{type(err).__name__}: {err}"))
        else:
            results.append(CheckResult(name, True, "; ".join(ctx.notes)))
    return results


@dataclass(frozen=True)
class Options:
    expect_state: Expect = Expect.READY
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    snapshot_timeout: float = DEFAULT_SNAPSHOT_TIMEOUT_SECONDS
    strict: bool = False
    after_start: bool = False


def run_contract(base_url: str, options: Options, extra_checks: list[Check] | None = None) -> list[CheckResult]:
    ctx = Context(Client(base_url, options.timeout), options.snapshot_timeout, options.strict)
    checks = {
        Expect.READY: READY_CHECKS,
        Expect.NOT_READY: NOT_READY_CHECKS,
        Expect.BACKGROUND: NOT_READY_CHECKS,
        Expect.STARTING: [check_start_sequence, *READY_CHECKS],
        Expect.STARTING_RACE: [check_start_race, *READY_CHECKS],
    }[options.expect_state]
    checks = ([check_after_start] if options.after_start else []) + checks + (extra_checks or [])
    initial: CameraStatus | None = None
    if options.expect_state is Expect.READY:
        # A starting app has no state to put back.
        try:
            initial = ctx.status()
        except ContractError, OSError:
            initial = None
    try:
        return run_checks(ctx, checks)
    finally:
        if initial is not None:
            restore(ctx, initial)


def print_results(results: list[CheckResult], label: str = "") -> bool:
    prefix = f"[{label}] " if label else ""
    for result in results:
        mark = "PASS" if result.passed else "FAIL"
        detail = f"  ({result.detail})" if result.detail else ""
        print(f"{prefix}{mark} {result.name}{detail}")
    failed = sum(not r.passed for r in results)
    print(f"{prefix}{len(results) - failed}/{len(results)} checks passed")
    return failed == 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default=os.environ.get(Env.BASE_URL, DEFAULT_BASE_URL))
    parser.add_argument(
        "--expect", type=Expect, choices=list(Expect), default=Expect.READY, help="camera state on the device"
    )
    parser.add_argument("--timeout", type=float, default=float(os.environ.get(Env.TIMEOUT, DEFAULT_TIMEOUT_SECONDS)))
    parser.add_argument(
        "--snapshot-timeout",
        type=float,
        default=float(os.environ.get(Env.SNAPSHOT_TIMEOUT, DEFAULT_SNAPSHOT_TIMEOUT_SECONDS)),
    )
    parser.add_argument(
        "--strict", action="store_true", help="also check the cases that the contract does not state (see docs/qa.md)"
    )
    parser.add_argument(
        "--after-start", action="store_true", help="the app just started: also check torch off and zoom at min"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    print(f"contract checks against {args.base_url} (expect {args.expect})")
    options = Options(args.expect, args.timeout, args.snapshot_timeout, args.strict, args.after_start)
    results = run_contract(args.base_url, options)
    return 0 if print_results(results) else 1


# endregion: runner

if __name__ == "__main__":
    sys.exit(main())
