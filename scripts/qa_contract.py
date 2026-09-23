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
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

# region: constants

ZOOM_STEP_FACTOR = 1.5
ZOOM_TOLERANCE = 1e-3
MAX_STEPS_TO_REACH_MAX_ZOOM = 64

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
    SNAPSHOT = "/v1/snapshot"
    UNKNOWN = "/v1/does-not-exist"


class Method(StrEnum):
    GET = "GET"
    POST = "POST"


class ContentType(StrEnum):
    JSON = "application/json"
    JPEG = "image/jpeg"


class ErrorCode(StrEnum):
    CAMERA_NOT_READY = "camera_not_ready"
    NO_FLASH_UNIT = "no_flash_unit"
    BAD_REQUEST = "bad_request"
    NOT_FOUND = "not_found"
    CAPTURE_FAILED = "capture_failed"


ERROR_STATUS: dict[ErrorCode, int] = {
    ErrorCode.CAMERA_NOT_READY: 503,
    ErrorCode.NO_FLASH_UNIT: 409,
    ErrorCode.BAD_REQUEST: 400,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.CAPTURE_FAILED: 500,
}

HTTP_OK = 200

CAMERA_STATUS_FIELDS: dict[str, type] = {
    "zoom_ratio": float,
    "min_zoom_ratio": float,
    "max_zoom_ratio": float,
    "torch_enabled": bool,
    "has_flash_unit": bool,
}
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


class ContractError(AssertionError):
    """One contract check failed."""


class Client:
    def __init__(self, base_url: str, timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def request(self, method: Method, path: str, body: bytes | None = None, timeout: float | None = None) -> Response:
        headers = {"Content-Type": ContentType.JSON} if body is not None else {}
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


def expect_status(resp: Response) -> CameraStatus:
    expect(resp.status == HTTP_OK, f"HTTP {resp.status}, expected {HTTP_OK}: {resp.body[:200]!r}")
    data = expect_json(resp)
    expect(isinstance(data, dict), f"CameraStatus is not an object: {data!r}")
    expect(
        set(data) == set(CAMERA_STATUS_FIELDS), f"CameraStatus keys {sorted(data)} != {sorted(CAMERA_STATUS_FIELDS)}"
    )
    for name, kind in CAMERA_STATUS_FIELDS.items():
        value = data[name]
        if kind is float:
            ok = isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)
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
]
STRICT_BAD_ZOOM_BODIES: list[tuple[str, bytes]] = [
    ("both ratio and step", b'{"ratio": 2, "step": "in"}'),
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


def check_not_found(ctx: Context) -> None:
    expect_error(ctx.client.get(Route.UNKNOWN), ErrorCode.NOT_FOUND)
    expect_error(ctx.client.post_json(Route.UNKNOWN, {}), ErrorCode.NOT_FOUND)


def check_not_ready(ctx: Context) -> None:
    check_health(ctx)
    expect_error(ctx.client.get(Route.STATUS), ErrorCode.CAMERA_NOT_READY)
    expect_error(ctx.client.post_json(Route.ZOOM, {"step": "in"}), ErrorCode.CAMERA_NOT_READY)
    expect_error(ctx.client.post_json(Route.TORCH, {"enabled": False}), ErrorCode.CAMERA_NOT_READY)
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
    check_torch,
    check_torch_bad_request,
    check_snapshot,
    check_not_found,
]
NOT_READY_CHECKS: list[Check] = [check_not_ready, check_not_found]

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


def run_contract(base_url: str, options: Options, extra_checks: list[Check] | None = None) -> list[CheckResult]:
    ctx = Context(Client(base_url, options.timeout), options.snapshot_timeout, options.strict)
    checks = READY_CHECKS if options.expect_state is Expect.READY else NOT_READY_CHECKS
    checks = checks + (extra_checks or [])
    initial: CameraStatus | None = None
    if options.expect_state is Expect.READY:
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
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    print(f"contract checks against {args.base_url} (expect {args.expect})")
    options = Options(args.expect, args.timeout, args.snapshot_timeout, args.strict)
    results = run_contract(args.base_url, options)
    return 0 if print_results(results) else 1


# endregion: runner

if __name__ == "__main__":
    sys.exit(main())
