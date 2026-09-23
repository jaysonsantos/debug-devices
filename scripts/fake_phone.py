#!/usr/bin/env python3
"""Fake phone: a stdlib-only HTTP server that implements docs/phone-api.md.

It keeps the camera state in memory, so the MCP server can run without a phone.

    python3 scripts/fake_phone.py                      # serve on 127.0.0.1:8765
    python3 scripts/fake_phone.py --no-flash           # torch returns 409 no_flash_unit
    python3 scripts/fake_phone.py --not-ready          # camera endpoints return 503 camera_not_ready
    python3 scripts/fake_phone.py --capture-fails      # snapshot returns 500 capture_failed
    python3 scripts/fake_phone.py --snapshot frame.jpg # serve this JPEG as the snapshot
    python3 scripts/fake_phone.py --self-check         # run scripts/qa_contract.py against every mode
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import sys
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from enum import StrEnum
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# region: constants

APP_VERSION = "0.1.0"
ZOOM_STEP_FACTOR = 1.5

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_MIN_ZOOM = 1.0
DEFAULT_MAX_ZOOM = 8.0
EPHEMERAL_PORT = 0
MAX_BODY_BYTES = 64 * 1024


class Env(StrEnum):
    HOST = "FAKE_PHONE_HOST"
    PORT = "FAKE_PHONE_PORT"
    MIN_ZOOM = "FAKE_PHONE_MIN_ZOOM"
    MAX_ZOOM = "FAKE_PHONE_MAX_ZOOM"
    SNAPSHOT = "FAKE_PHONE_SNAPSHOT"


class Route(StrEnum):
    HEALTH = "/v1/health"
    STATUS = "/v1/status"
    ZOOM = "/v1/zoom"
    TORCH = "/v1/torch"
    SNAPSHOT = "/v1/snapshot"


class ContentType(StrEnum):
    JSON = "application/json"
    JPEG = "image/jpeg"


class ZoomStep(StrEnum):
    IN = "in"
    OUT = "out"


class ErrorCode(StrEnum):
    CAMERA_NOT_READY = "camera_not_ready"
    NO_FLASH_UNIT = "no_flash_unit"
    BAD_REQUEST = "bad_request"
    NOT_FOUND = "not_found"
    CAPTURE_FAILED = "capture_failed"


ERROR_STATUS: dict[ErrorCode, HTTPStatus] = {
    ErrorCode.CAMERA_NOT_READY: HTTPStatus.SERVICE_UNAVAILABLE,
    ErrorCode.NO_FLASH_UNIT: HTTPStatus.CONFLICT,
    ErrorCode.BAD_REQUEST: HTTPStatus.BAD_REQUEST,
    ErrorCode.NOT_FOUND: HTTPStatus.NOT_FOUND,
    ErrorCode.CAPTURE_FAILED: HTTPStatus.INTERNAL_SERVER_ERROR,
}

# A 64x48 test pattern (ffmpeg testsrc2). Used when --snapshot is not given.
TEST_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAgAAAQABAAD//gAPTGF2YzYzLjEuMTAxAP/bAEMACBAQExATFhYWFhYWGhgaGxsbGhoaGhsbGx0dHSIi"
    "Ih0dHRsbHR0gICIiJSYlIyMiIyYmKCgoMDAuLjg4OkVFU//EAJEAAAMBAQEBAAAAAAAAAAAAAAQFBgMHAAgBAQEBAQEAAAAA"
    "AAAAAAAAAAYFBAgHEAABAwICCAMGBwEBAAAAAAABAhIRAwAEIVETFEFhMjEFM4JCQyKxgcFTI3LSUhWycaOiEQACAAUCBAQE"
    "BwEAAAAAAAABAgMRIQAEMRIiBTJBUWFyYnGxE9EGM9KBsnNCUv/AABEIADAAQAMBIgACEQADEQD/2gAMAwEAAhEDEQA/AOXE"
    "111VIp7g48oAAEklSsgOJNsqNTHUlqQkwSkL9mUsglz+VvGY3XrSIVtdNr1K1KhTmDUTTLlgHS3RnoFu0qRsmzs/E8TZ3FzN"
    "a9jv3M3c/C5ednPlNxJNd1FY7toYAzqRwjTQCmtpsjHBSPCZyykmY/y2x6LtqJkimp8Ba8dwx+HqJmo0xILaZBBHUEJII4i2"
    "6O79zqGE1pyJPuUQABvJKIA/209Ut2RAiktKlqaTOrC1yhxOgdZz0iyaJcMSgxUWpSVQD4jVkqaRpHSM9AuCyQyNxhQyfSP+"
    "pT+Eq63AgQEhEonCJzkABX6YMqAcU+Hpn5drhdnxL2NzZrJlDWRL3yxvGYnLre6VYjCqKT7pUEq9KgoGYIIkEcQbsnI2PZWf"
    "i+Jszi7V67WMd1czdzxnFqKymJwlNMUaiFLU1RnUipUch5IPQZmc46i3Scyyoh2szy3MvU1QoJETqPCSJaEe43SSCsJgysQQ"
    "JzFJE0KaCoB8QfK88NisYqoNWrMAmYRAAGZJIgDibfbb3JzH5tfy0mtiXOhreMxazCqdtaFEVVrUhUJMa1iyVtIA6jMRnHQX"
    "QOTs2oZ7/PqJLmax7Z/czdzcLnx82MHlU1VdSZAgHfVhwgmWgHuF4crEhZbh43Gwh0ZxuJkxlDE1ap1EiT7b5xifFV8vgLyp"
    "771xPiq+XwF5U995R0D4C6XMdY/9h/nZyOYWdYKOYWdeVtbNQek/G46yqe+xbKp77cNpeuF1j9/lZyOYWbYSOYWbeFtbxZn5"
    "g9I+Zv1ehUXUUQmQY3jQON60MFiKjmomI9SeOk27ul7d7Ty/WxETIZIdAtJeP3t/z7HTHwcnIUsXBQyMtvFFUGgAPelbmKXa"
    "8YtYApSTPrp6PzW5/hO4fY/6Uv130bB+Ojzf1N3NmI3MYyMAFh6eDfqvzblkQ5EFmaQIcinpU95+N/FTFaPhbChh6tRzUzEb"
    "xx0m/XTdu9p5frfUcXlsFUJ3RO3dfH03cwF+tkw0agO7TWik+dhUsBiVrAFOSZ9SNH5rcfxWN+1/7p/quuwfjo839TdvZaNj"
    "IjAAtp5fazX4jjNgZcOHDAYGCr8dTMu47FaUv//Z"
)

# endregion: constants

# region: state


@dataclass
class CameraStatus:
    zoom_ratio: float
    min_zoom_ratio: float
    max_zoom_ratio: float
    torch_enabled: bool
    has_flash_unit: bool


@dataclass(frozen=True)
class FakeConfig:
    min_zoom: float = DEFAULT_MIN_ZOOM
    max_zoom: float = DEFAULT_MAX_ZOOM
    has_flash_unit: bool = True
    ready: bool = True
    capture_fails: bool = False
    snapshot: bytes = TEST_JPEG


class ApiError(Exception):
    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class FakeCamera:
    """In-memory camera. All methods are thread safe."""

    def __init__(self, config: FakeConfig) -> None:
        self.config = config
        self._lock = threading.Lock()
        self._status = CameraStatus(
            zoom_ratio=config.min_zoom,
            min_zoom_ratio=config.min_zoom,
            max_zoom_ratio=config.max_zoom,
            torch_enabled=False,
            has_flash_unit=config.has_flash_unit,
        )

    def _require_ready(self) -> None:
        if not self.config.ready:
            raise ApiError(ErrorCode.CAMERA_NOT_READY, "Camera is not bound yet")

    def _clamp(self, ratio: float) -> float:
        return min(max(ratio, self._status.min_zoom_ratio), self._status.max_zoom_ratio)

    def status(self) -> CameraStatus:
        self._require_ready()
        with self._lock:
            return CameraStatus(**asdict(self._status))

    def zoom_ratio(self, ratio: float) -> CameraStatus:
        self._require_ready()
        with self._lock:
            self._status.zoom_ratio = self._clamp(ratio)
        return self.status()

    def zoom_step(self, step: ZoomStep) -> CameraStatus:
        self._require_ready()
        with self._lock:
            current = self._status.zoom_ratio
            wanted = current * ZOOM_STEP_FACTOR if step is ZoomStep.IN else current / ZOOM_STEP_FACTOR
            self._status.zoom_ratio = self._clamp(wanted)
        return self.status()

    def torch(self, enabled: bool) -> CameraStatus:
        self._require_ready()
        if not self._status.has_flash_unit:
            raise ApiError(ErrorCode.NO_FLASH_UNIT, "This camera has no flash unit")
        with self._lock:
            self._status.torch_enabled = enabled
        return self.status()

    def snapshot(self) -> bytes:
        self._require_ready()
        if self.config.capture_fails:
            raise ApiError(ErrorCode.CAPTURE_FAILED, "Still capture failed (fake)")
        return self.config.snapshot


# endregion: state

# region: http


def parse_body(raw: bytes) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as err:
        raise ApiError(ErrorCode.BAD_REQUEST, f"Body is not valid JSON: {err}") from err
    if not isinstance(data, dict):
        raise ApiError(ErrorCode.BAD_REQUEST, "Body must be a JSON object")
    return data


def is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


class Handler(BaseHTTPRequestHandler):
    camera: FakeCamera  # set by make_server
    quiet: bool = False

    def log_message(self, format: str, *args: Any) -> None:
        if not self.quiet:
            super().log_message(format, *args)

    def _send(self, status: HTTPStatus, content_type: ContentType, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        self._send(status, ContentType.JSON, json.dumps(payload).encode())

    def _send_error(self, err: ApiError) -> None:
        self._send_json({"error": err.code, "message": err.message}, ERROR_STATUS[err.code])

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            raise ApiError(ErrorCode.BAD_REQUEST, f"Body is larger than {MAX_BODY_BYTES} bytes")
        return self.rfile.read(length)

    def _dispatch(self, routes: dict[str, Callable[[], None]]) -> None:
        route = routes.get(self.path.split("?", 1)[0])
        try:
            if route is None:
                raise ApiError(ErrorCode.NOT_FOUND, f"No route for {self.command} {self.path}")
            route()
        except ApiError as err:
            self._send_error(err)

    def do_GET(self) -> None:
        self._dispatch(
            {
                Route.HEALTH: self.health,
                Route.STATUS: lambda: self._send_json(asdict(self.camera.status())),
                Route.SNAPSHOT: self.snapshot,
            }
        )

    def do_POST(self) -> None:
        self._dispatch({Route.ZOOM: self.zoom, Route.TORCH: self.torch})

    def health(self) -> None:
        self._send_json({"ok": True, "app_version": APP_VERSION})

    def zoom(self) -> None:
        data = parse_body(self._read_body())
        has_ratio, has_step = "ratio" in data, "step" in data
        if has_ratio == has_step:
            raise ApiError(ErrorCode.BAD_REQUEST, 'Send exactly one of "ratio" or "step"')
        if has_ratio:
            if not is_number(data["ratio"]):
                raise ApiError(ErrorCode.BAD_REQUEST, '"ratio" must be a finite number')
            status = self.camera.zoom_ratio(float(data["ratio"]))
        else:
            try:
                step = ZoomStep(data["step"])
            except ValueError as err:
                raise ApiError(ErrorCode.BAD_REQUEST, '"step" must be "in" or "out"') from err
            status = self.camera.zoom_step(step)
        self._send_json(asdict(status))

    def torch(self) -> None:
        data = parse_body(self._read_body())
        enabled = data.get("enabled")
        if not isinstance(enabled, bool):
            raise ApiError(ErrorCode.BAD_REQUEST, '"enabled" must be true or false')
        self._send_json(asdict(self.camera.torch(enabled)))

    def snapshot(self) -> None:
        self._send(HTTPStatus.OK, ContentType.JPEG, self.camera.snapshot())


def make_server(host: str, port: int, config: FakeConfig, quiet: bool = False) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"camera": FakeCamera(config), "quiet": quiet})
    return ThreadingHTTPServer((host, port), handler)


# endregion: http

# region: self-check


def self_check() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import qa_contract  # noqa: PLC0415 (only the self-check needs it)

    scenarios: list[tuple[str, FakeConfig, qa_contract.Expect, list[qa_contract.Check]]] = [
        ("default", FakeConfig(), qa_contract.Expect.READY, []),
        ("fixed zoom", FakeConfig(min_zoom=1.0, max_zoom=1.0), qa_contract.Expect.READY, []),
        ("no flash", FakeConfig(has_flash_unit=False), qa_contract.Expect.READY, []),
        ("not ready", FakeConfig(ready=False), qa_contract.Expect.NOT_READY, []),
        (
            "capture fails",
            FakeConfig(capture_fails=True),
            qa_contract.Expect.READY,
            [qa_contract.check_capture_failed],
        ),
    ]
    all_passed = True
    for label, config, expect_state, extra in scenarios:
        server = make_server(DEFAULT_HOST, EPHEMERAL_PORT, config, quiet=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://{DEFAULT_HOST}:{server.server_address[1]}"
            results = qa_contract.run_contract(
                base_url, qa_contract.Options(expect_state=expect_state, strict=True), extra
            )
            if label == "capture fails":
                # The normal snapshot check must fail in this mode; the extra check covers it.
                results = [r for r in results if r.name != "snapshot"]
            all_passed &= qa_contract.print_results(results, label)
        finally:
            server.shutdown()
            server.server_close()
    print("self-check", "PASSED" if all_passed else "FAILED")
    return 0 if all_passed else 1


# endregion: self-check

# region: cli


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default=os.environ.get(Env.HOST, DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(os.environ.get(Env.PORT, DEFAULT_PORT)))
    parser.add_argument("--min-zoom", type=float, default=float(os.environ.get(Env.MIN_ZOOM, DEFAULT_MIN_ZOOM)))
    parser.add_argument("--max-zoom", type=float, default=float(os.environ.get(Env.MAX_ZOOM, DEFAULT_MAX_ZOOM)))
    parser.add_argument("--no-flash", action="store_true", help="simulate a camera without a flash unit")
    parser.add_argument("--not-ready", action="store_true", help="camera endpoints return 503 camera_not_ready")
    parser.add_argument("--capture-fails", action="store_true", help="snapshot returns 500 capture_failed")
    parser.add_argument(
        "--snapshot",
        type=Path,
        default=os.environ.get(Env.SNAPSHOT),
        help="JPEG file to serve as the snapshot (default: built-in 64x48 test pattern)",
    )
    parser.add_argument("--quiet", action="store_true", help="do not log requests")
    parser.add_argument("--self-check", action="store_true", help="run the contract checks against every mode")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.self_check:
        return self_check()
    if args.min_zoom <= 0 or args.min_zoom > args.max_zoom:
        print(f"bad zoom range [{args.min_zoom}, {args.max_zoom}]", file=sys.stderr)
        return 2
    config = FakeConfig(
        min_zoom=args.min_zoom,
        max_zoom=args.max_zoom,
        has_flash_unit=not args.no_flash,
        ready=not args.not_ready,
        capture_fails=args.capture_fails,
        snapshot=Path(args.snapshot).read_bytes() if args.snapshot else TEST_JPEG,
    )
    server = make_server(args.host, args.port, config, args.quiet)
    print(
        f"fake phone on http://{args.host}:{server.server_address[1]}"
        f" zoom=[{config.min_zoom}, {config.max_zoom}] flash={config.has_flash_unit} ready={config.ready}"
        f" capture_fails={config.capture_fails} snapshot={len(config.snapshot)} bytes",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


# endregion: cli

if __name__ == "__main__":
    sys.exit(main())
