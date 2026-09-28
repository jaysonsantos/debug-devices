#!/usr/bin/env python3
"""Fake phone: a stdlib-only HTTP server that implements docs/phone-api.md.

It keeps the camera state in memory, so the MCP server can run without a phone.

    python3 scripts/fake_phone.py                      # serve on 127.0.0.1:8765
    python3 scripts/fake_phone.py --no-flash           # torch returns 409 no_flash_unit
    python3 scripts/fake_phone.py --not-ready          # camera endpoints return 503 camera_not_ready
    python3 scripts/fake_phone.py --capture-fails      # snapshot returns 500 capture_failed
    python3 scripts/fake_phone.py --background         # the app is in the background: camera endpoints return 503
    python3 scripts/fake_phone.py --physical-rotation 90 # auto rotation follows this phone orientation
    python3 scripts/fake_phone.py --internal-error     # camera endpoints return 500 internal_error
    python3 scripts/fake_phone.py --no-preview         # an old app without POST /v1/preview
    python3 scripts/fake_phone.py --start-delay 3      # camera endpoints return 503 for 3 s, then the start state
    python3 scripts/fake_phone.py --snapshot frame.jpg # serve this JPEG as the snapshot
    python3 scripts/fake_phone.py --self-check         # run scripts/qa_contract.py against every mode
"""

from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import math
import os
import re
import sys
import threading
import time
import uuid
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
SELF_CHECK_START_DELAY = 0.5
# A phone with an ultrawide lens: the zoom range starts below the main camera (1.0).
ULTRAWIDE_MIN_ZOOM = 0.6
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
    ROTATION = "/v1/rotation"
    PREVIEW = "/v1/preview"
    CAMERA = "/v1/camera"
    FOCUS = "/v1/focus"
    OVERLAY = "/v1/overlay"
    FAKE_FOCUS = "/fake/focus"
    SNAPSHOT = "/v1/snapshot"


KNOWN_PATHS = frozenset(Route)
# An old app (from before POST /v1/preview) has none of these paths: 404.
NEWER_PATHS = frozenset({Route.PREVIEW, Route.CAMERA, Route.FOCUS, Route.OVERLAY})


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
    METHOD_NOT_ALLOWED = "method_not_allowed"
    CAPTURE_FAILED = "capture_failed"
    INTERNAL_ERROR = "internal_error"


ERROR_STATUS: dict[ErrorCode, HTTPStatus] = {
    ErrorCode.CAMERA_NOT_READY: HTTPStatus.SERVICE_UNAVAILABLE,
    ErrorCode.NO_FLASH_UNIT: HTTPStatus.CONFLICT,
    ErrorCode.BAD_REQUEST: HTTPStatus.BAD_REQUEST,
    ErrorCode.NOT_FOUND: HTTPStatus.NOT_FOUND,
    ErrorCode.METHOD_NOT_ALLOWED: HTTPStatus.METHOD_NOT_ALLOWED,
    ErrorCode.CAPTURE_FAILED: HTTPStatus.INTERNAL_SERVER_ERROR,
    ErrorCode.INTERNAL_ERROR: HTTPStatus.INTERNAL_SERVER_ERROR,
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

# TEST_JPEG turned 90 degrees clockwise (ffmpeg transpose=clock), 48x64, no EXIF.
TEST_JPEG_TURN_90 = base64.b64decode(
    "/9j/4AAQSkZJRgABAgAAAQABAAD/2wBDAAgQEBMQExYWFhYWFhoYGhsbGxoaGhobGxsdHR0iIiIdHR0bGx0dICAiIiUmJSMj"
    "IiMmJigoKDAwLi44ODpFRVP/xACWAAACAwEBAQAAAAAAAAAAAAAGBwAEBQgBAwEBAQEAAwEAAAAAAAAAAAAABQYEAgEDCBAA"
    "AQMCAwYEBAQHAQAAAAAAAQIREgADEwQhUUEiYRRSMQUyotEVYqFj4UIzNTSys4PwgnERAAIABQIEBAMJAQAAAAAAAAECEQMh"
    "AAQSUUExIjIFE5FhQoFSocGycQYz0VMUFf/AABEIAEAAMAMBIgACEQADEQD/2gAMAwEAAhEDEQA/AAuh+/fuIuKAUwDbhsHK"
    "mL8u/E9v50v87Yw8wtMnaO76QdtS2PMluxHPp29xdpkePYOQgXHySXBiYJNWkCDUoOJFLz+pu932HwqdTd7vsPhXyw+dTD50"
    "vBNh6WR/0T/fN9XsqFqIViZuKkNiBNk3E2yfAFaeF93/AK4rIBuov4a1A80sQQUukgjcQxFGSU5SC+o/c4eow8WEn4Z4fBLb"
    "9b0P27K7ueCVqSCewCEQjhix9MWbl41xwc5sV3bq0iDMKKG0wNICq1PONCL05GQgCNNnkKpqYu+lfi1CpBgOW4It0dHf7Pcn"
    "40oPNLS0Zy6CGIhvHYK6YpAed/xC/wD4/wC2moPw6czzmBA7D+JbhZfhknHOpWckiFSv58FG1g0FbKkFbKvVKtNRvZ5K7m9g"
    "XZaLyilLuAFUVrQLjahUBodr7XNe+Xi5mM/bMQ6pMAwAAQQAOQAYVYFkJ1XmVJXbABilasPcEyBYbG2uKv8AlCDb8zspLaT1"
    "BcEG0ogjkRrRLsBLmleYRvq24RpDlyvfPkGamhyYNEGHlxqCKwEdVW7uMfe1pWev1H/d1G3y78T2/nQzmLGHdUmTs27kDtr6"
    "F8Smo0lYH4xwP0m+2wMmT1PLgDTuU1+R9ryalWsPnUw+dRGoX5eU+32i2Ono4L6r93h6nDx8OT8M8Lgk/jum7UDZifUqnB9G"
    "w2hGAjBv0xZt7eOtESbyV8NzKKXcugKVFdy2LzOQuCdD4O40dzWIlKs1mHUQkq7UsEhKWAAfwAAAovw1DLylZtUNS9zBgOof"
    "tgEkLQ8+AXa1pyNNVVUAkmAhQmMe8mAJqPmTbW6O/wBnuT8aXOftLRmbgIYiO8dgp+0mvNf527/x/QKosmczoAQO77jcVJ/U"
    "WXnsZcyXIAA19CuDEU4zDStiEFbKkFbKu1KD1G0v9kzZfQ/ze4nKxZS82pC7YCTFC14TuAmYLDxZho7iqmVy1xOcTbYE6sxB"
    "BECQQdhGoorT00V4/r0x4YkJPpLD4JbfqeqWSl8yROL8XoaMcMxi36Ys3KicKe5nVjQjuUAGBB6IAErU8+BXe1cvKfElecgl"
    "Fki6jqMCsTCYI0NAIDiGv//Z"
)
# TEST_JPEG turned 180 degrees (ffmpeg hflip,vflip), 64x48, no EXIF.
TEST_JPEG_TURN_180 = base64.b64decode(
    "/9j/4AAQSkZJRgABAgAAAQABAAD/2wBDAAgQEBMQExYWFhYWFhoYGhsbGxoaGhobGxsdHR0iIiIdHR0bGx0dICAiIiUmJSMj"
    "IiMmJigoKDAwLi44ODpFRVP/xACRAAADAQEBAQAAAAAAAAAAAAAEBQYDAAcIAQEBAQEBAAAAAAAAAAAAAAAGBQQIBxAAAQMC"
    "AggDBgcBAQAAAAAAAQISEQMABCFRExRBYTIxBTOCQkMisYHBUyNy0lIVsnGjohEAAgAFAgQEBAcBAAAAAAAAAQIDESEABDES"
    "IgUyQVFhcmJxsRPRBjPSgbJzQlL/wAARCAAwAEADASIAAhEAAxEA/9oADAMBAAIRAxEAPwCzuIxnjr8v9Rcj/K437v8A4p/p"
    "tPVx+JWsk1JJj0o0fltVByURiSG08vvcP8OwWwMuJEiEMDBZOCpmXQ9wtKWb3H2fm+lzN9XxFWo1ypidw4aBa96tPwtTC5lB"
    "VANsTv2Xx9VpM9vrZMR1oDt11ooHnf2rcNjPHX5f6i/Of5vuH3/+dL9FpqvdMYtZJqyTHop6Py3y5B5dGRiS0PTxb9Nw+Zwz"
    "kQVVZAhwa+lh2n43T9x9n5vpc1aSvjcRUa5cxPpTw0C8qFeouokFUgzuGg8LTw8dkh1K0n4/a/SOQ5CY+DjY7Bi4LiYlt4or"
    "EVJB71pfWEvmNm2EvmNt11sDh/mH0n5iwam6xbKqbrFvcul7YvWf2+V2Ngr5jZ1gr5jYddbyRukfGwam69cN4qfn8DeVTdeu"
    "G8VPz+BvUeg/A2l5drA/sH879HanZte/3+TXwXM1jHR+5m/m43P4pLtkWkCqtalplQjWsWAhwJHUZGc46m2exdyc9mbWc1Jr"
    "YhrZa3hEWhxOFxiqh1icwAIlEAAZAAGAOAvVAwowedTVm0JkCCNlFHCCZ6ge0XNxcuFluUg8bCHVUO4mTCcQyZqnQzBPuvSi"
    "licXUVFGohSEuSJ1IqVGrYAT0GQjOOhtu1Gx7U/8Xw9paXavXat7ermb+eMpuNUnEYVQUPdKgpPpUFAxIIMgjgReG0Yl73Zs"
    "1cQhrIhjIY3hETn1ug/LcqIdyq8tyt0tUKADD6TwkiepHtN7njLCYqykECUjSRNQ+oqAfAHzu6rBwwyxFRalKTJHiNWAlwOk"
    "dZz0mxqQbtazFJaVIS4CdWFrhbQNA6RnoNuF9o7nUMqozkAPfogADcAFwB/lqD2/H4eoqKbTEEOpkEEdCCogjgbCq8MjaIsM"
    "n1D/AKnL4Sppc2PHSEQ78InKZIFfpkTqRxT4uqfn3tgpKNk2h/4nh7Q0uZrWPb+5m/n42kqgK2So56la5JqRBqJplqCRpbpz"
    "0m8q1PHUlpWoQSko9mUsgBrOVvCI32tArrqpXU3Bo5QAAIACU5AcALvYOC+U3C813VZRu2hgRKgHCNdQKaXfx8gFIEVULKSJ"
    "H/LbHq26omSK6nxN/wD/2Q=="
)
# TEST_JPEG turned 270 degrees clockwise (ffmpeg transpose=cclock), 48x64, no EXIF.
TEST_JPEG_TURN_270 = base64.b64decode(
    "/9j/4AAQSkZJRgABAgAAAQABAAD/2wBDAAgQEBMQExYWFhYWFhoYGhsbGxoaGhobGxsdHR0iIiIdHR0bGx0dICAiIiUmJSMj"
    "IiMmJigoKDAwLi44ODpFRVP/xACWAAACAwEBAQAAAAAAAAAAAAAGBwAEBQgBAwEBAQEAAwEAAAAAAAAAAAAABQYEAgEDCBAA"
    "AQMCAwYEBAQHAQAAAAAAAQIREgADEwQhUUEiYRRSMQUyotEVYqFj4UIzNTSys4PwgnERAAIABQIEBAMJAQAAAAAAAAECEQMh"
    "AAQSUUExIjIFE5FhQoFSocGycQYz0VMUFf/AABEIAEAAMAMBIgACEQADEQD/2gAMAwEAAhEDEQA/ABrOy+ZLhF+H1tGOGJSf"
    "9MXflV1XTRRgevXAniQk+scTgls+pqFM1mbic4q44J0dwCCIAEEbCNDVtWai6UZRSF2wVCS1rwnYlUCGHi7nR2NZ82Q5nUjQ"
    "ntYAGBI64kErUcuBbe9eJiviSvJcyiyQRj1GBWAjLMKGhMTxC3h1KpTVtqTVtpbSbK/xzN19T/Fl/lX87a/7/oNOWkFkLq0Z"
    "m2QWIluHYaY3WX+/2p+FOY0lnQkEd33CzZ36dy89hMlzJAAGjrZwYivCWaVtUqUrNZhkgJKu5TBISlySW8AASa21WUr4rebU"
    "u5dBSmSLlsXmYFE1aHwZjo7Ch3Lz6lMIPq+I0IwMpv8Api7728NaOVdHBHS/u8XTYmPhyfihi8En8N02ep3xJzLymVdUNTdq"
    "hgOo/uEgkLQcuAba7WS7TVZmIJJiY0JjDsAgCan5kWuMTlUxOVValKaRZPmvv9gvWy9/DupVF2ffyI2UTfMfw/d+VBKPUP8A"
    "d1aFW/hspGktEfGeJ+kX6rn5MnpSZAGvapr8x7Wy/N1m35neUG0hoQ4INpIIPIjSqBvBOiMspK7YJElLVh7yqJDDa+1jVfzA"
    "3Mxn7gkHVFyWAACASTyADmvDalqjNqUu4CEyQtAuNoUzOh2NtYV89IoEuUG5hF+rbjCkOfO+pE8zU1oDBoER8uNQDSJjqqvb"
    "xh7Xj1KozVtqTVtpbSbwecuxs58k/iFj/J/bVT/rmfyu6tGctEFiJ7h2Gm/1l/v9qfhUX4jJZ5ykEdg/E145nicnHOllckiN"
    "Av5cWG1pe5eXdzxUhKQT3kQiEcUnHpi78vCiBSspBHT/ALnF0+Jiwk/FDE4JbPrag0i6i/iISDyUxBBSygQdxDg1rm7EJw8p"
    "FSHwyq8bibZPiQhXC+//ANY1eZ2C2K6L1aRFVNFDaYisTVajlGhN3WPjoA6ypBCqaCDvpX4dJqQYDnuAbFcTlUxOVfXprvb9"
    "x8anTXe37j41yim49bzf84/0TfR70Mlfw8whUXaW/wCkjZTA+Y/h+78qXVixcRcSSlgH3jYedEFEZEuW7A8+nf3Nr4/gODkI"
    "WyMYlwYCLzVpAEUDjiTW/wD/2Q=="
)

ROTATIONS = (0, 90, 180, 270)
# The back camera sensor of most phones: its image is turned 90 degrees against the natural portrait screen.
# The configured snapshot is the raw sensor image (landscape). Like the app, the fake turns the pixels (rotation 0,
# portrait, gives a portrait JPEG) and writes no EXIF orientation (docs/phone-api.md).
SENSOR_ORIENTATION = 90
FULL_TURN = 360
# The built-in sensor image, turned clockwise by 0, 90, 180, and 270 degrees.
TURNED_TEST_JPEGS = {0: TEST_JPEG, 90: TEST_JPEG_TURN_90, 180: TEST_JPEG_TURN_180, 270: TEST_JPEG_TURN_270}


PILLOW_MODULE = "PIL"


class Header(StrEnum):
    """The /v1/snapshot response headers."""

    ROTATION_DEGREES = "X-Rotation-Degrees"
    APP_START_ID = "X-App-Start-Id"


def turn_jpeg(jpeg: bytes, turn: int) -> bytes:
    """The still turned clockwise by `turn`. The built-in image has fixed turns; another file needs Pillow."""
    if jpeg == TEST_JPEG:
        return TURNED_TEST_JPEGS[turn]
    if turn == 0:
        return jpeg
    try:
        import io  # noqa: PLC0415 (only for a --snapshot file)

        from PIL import Image  # noqa: PLC0415 (optional: the fake stays stdlib-only)
    except ImportError:
        return jpeg  # main() warns: without Pillow, a --snapshot file is not turned
    with Image.open(io.BytesIO(jpeg)) as image:
        turned = image.rotate(-turn, expand=True)
        output = io.BytesIO()
        turned.convert("RGB").save(output, format="JPEG", quality=90)
    return output.getvalue()


# endregion: constants

# region: state


# The autofocus mode after an app start (CameraStatus.af_mode).
AF_CONTINUOUS = "continuous"
# The contract example: a 9:20 portrait screen shows the middle 60 % of a 3:4 still.
PREVIEW_REGION = {"snapshot_x": 0.2, "snapshot_y": 0.0, "width": 0.6, "height": 1.0}
# With rotation 90 or 270, the still is the same camera image turned by a quarter turn: the cut moves to the other axis.
PREVIEW_REGION_SIDEWAYS = {"snapshot_x": 0.0, "snapshot_y": 0.2, "width": 1.0, "height": 0.6}
SIDEWAYS_ROTATIONS = frozenset({90, 270})
# The safe area of the fake screen (docs/overlay-layout.md) in the natural portrait frame of the preview: the status bar
# and the app's status label on top, the navigation bar at the bottom. Not symmetric, so a vertical flip moves it.
# At rotation 0 with no flip, this gives the contract example {0.2, 0.04, 0.6, 0.9}.
SAFE_TOP = 0.04
SAFE_BOTTOM = 0.06
REGION_DIGITS = 6


def still_point(
    u: float, v: float, rotation_degrees: int, flip_horizontal: bool, flip_vertical: bool
) -> tuple[float, float]:
    """A point of the preview on the screen (natural portrait, 0..1) as a point of the still (0..1 in the region)."""
    if flip_horizontal:
        u = 1 - u
    if flip_vertical:
        v = 1 - v
    # The still is the screen image turned clockwise by (360 - rotation_degrees) % 360 (docs/phone-api.md).
    match (FULL_TURN - rotation_degrees) % FULL_TURN:
        case 90:
            return 1 - v, u
        case 180:
            return 1 - u, 1 - v
        case 270:
            return v, 1 - u
        case _:
            return u, v


def overlay_region(
    preview: dict[str, float] | None, rotation_degrees: int, flip_horizontal: bool, flip_vertical: bool
) -> dict[str, float] | None:
    """The safe area of the screen, mapped into preview_region on the still. Same null rule as preview_region."""
    if preview is None:
        return None
    corners = [(0.0, SAFE_TOP), (1.0, 1.0 - SAFE_BOTTOM)]
    points = [still_point(u, v, rotation_degrees, flip_horizontal, flip_vertical) for u, v in corners]
    left, right = sorted(x for x, _ in points)
    top, bottom = sorted(y for _, y in points)
    region = {
        "snapshot_x": preview["snapshot_x"] + left * preview["width"],
        "snapshot_y": preview["snapshot_y"] + top * preview["height"],
        "width": (right - left) * preview["width"],
        "height": (bottom - top) * preview["height"],
    }
    return {name: round(value, REGION_DIGITS) for name, value in region.items()}


# The main camera. The app starts at this zoom when it is inside [min, max], else at min (ultrawide phones).
MAIN_CAMERA_ZOOM = 1.0


def start_zoom(min_zoom: float, max_zoom: float) -> float:
    return MAIN_CAMERA_ZOOM if min_zoom <= MAIN_CAMERA_ZOOM <= max_zoom else min_zoom


@dataclass
class CameraStatus:
    zoom_ratio: float
    min_zoom_ratio: float
    max_zoom_ratio: float
    torch_enabled: bool
    has_flash_unit: bool
    rotation_degrees: int
    rotation_locked: bool
    preview_flip_horizontal: bool = False
    preview_flip_vertical: bool = False
    focus: dict | None = None
    optics: dict | None = None
    in_sensor_zoom: str = "off"
    overlay_boxes: int = 0
    overlay_arrows: int = 0
    af_mode: str = AF_CONTINUOUS
    # A UUID v7 per app start (set by FakeCamera); an old app (--no-preview) does not send it.
    app_start_id: str = ""
    # The part of the still that the fake preview shows: a 9:20 screen filled from a 3:4 image (the middle 60 %).
    preview_region: dict[str, float] | None = None
    # Where the app draws boxes: preview_region without the bars and the status label. It moves with a vertical flip.
    overlay_region: dict[str, float] | None = None
    # False while the boxes and arrows are hidden (kept); true after an app start.
    overlay_visible: bool = True


# The body of POST /v1/preview: exactly these fields, both booleans.
PREVIEW_FIELDS = frozenset({"flip_horizontal", "flip_vertical"})
# An old app (from before POST /v1/preview) sends none of these status fields.
NEWER_STATUS_FIELDS = (
    "preview_flip_horizontal",
    "preview_flip_vertical",
    "focus",
    "optics",
    "in_sensor_zoom",
    "overlay_boxes",
    "overlay_arrows",
    "af_mode",
    "app_start_id",
    "preview_region",
    "overlay_region",
    "overlay_visible",
)
# The body of POST /v1/camera: exactly this field, a boolean.
CAMERA_FIELD = "in_sensor_zoom"
# POST /v1/camera can also set the autofocus mode. The body needs at least one of the two fields.
AF_MODE_FIELD = "af_mode"
CAMERA_FIELDS = frozenset({CAMERA_FIELD, AF_MODE_FIELD})
AF_MODES = frozenset({AF_CONTINUOUS, "macro"})
IN_SENSOR_ZOOM_ON = "on"
IN_SENSOR_ZOOM_OFF = "off"
IN_SENSOR_ZOOM_UNSUPPORTED = "unsupported"
# Plausible values of a phone main camera (docs/phone-api.md example): about 29 cm from the board.
DEFAULT_FOCUS_DIOPTERS = 3.41
MIN_FOCUS_DIOPTERS = 10.0
FOCUS_STATE = "focused"
FOCUS_CALIBRATION = "approximate"
OPTICS = {"focal_length_mm": 6.07, "sensor_width_mm": 9.14, "output_width_px": 4080}
# The body of POST /v1/focus: exactly one of these pairs, each value a number in [0, 1].
SCREEN_FOCUS_FIELDS = frozenset({"screen_x", "screen_y"})
SNAPSHOT_FOCUS_FIELDS = frozenset({"snapshot_x", "snapshot_y"})
# The fake camera preview on the phone screen (natural portrait, from 0 to 1): the status bar above it and the
# controls below it are outside the preview.
PREVIEW_SCREEN_TOP = 0.1
PREVIEW_SCREEN_BOTTOM = 0.8
OUTSIDE_PREVIEW = "outside the preview"
FOCUS_FIELDS_MESSAGE = 'Send exactly "screen_x" and "screen_y", or "snapshot_x" and "snapshot_y"'
# After POST /v1/focus, the state is "scanning" for this time, then "focused".
FOCUS_SCAN_SECONDS = 0.3
FOCUS_SCANNING = "scanning"
# POST /v1/overlay: at most this many boxes, each with exactly these fields, labels at most 32 characters.
OVERLAY_MAX_BOXES = 8
OVERLAY_MAX_LABEL = 32
# An optional short tag per box (docs/overlay-layout.md).
TAG_FIELD = "tag"
# The tag rule of docs/phone-api.md: 1 to 3 ASCII letters or digits (the same count on every side).
TAG_PATTERN = re.compile(r"[A-Za-z0-9]{1,3}")
OVERLAY_BOX_FIELDS = frozenset({"snapshot_x", "snapshot_y", "width", "height", "label"})
# x + width and y + height may be this much above 1 (float rounding).
# "Inside the image" (docs/phone-api.md): x >= 0 and y >= 0 strict; x + width and y + height up to 1 + this tolerance.
OVERLAY_TOLERANCE = 1e-4
# Optional arrows: at most this many, each with exactly these fields.
OVERLAY_MAX_ARROWS = 4
OVERLAY_ARROW_FIELDS = frozenset({"angle_deg", "label"})
# POST /v1/overlay {"visible": false} hides the boxes and arrows without removing them.
VISIBLE_FIELD = "visible"
# A test-only route (not in the contract): change the focus distance, like moving the phone.
FAKE_FOCUS_FIELD = "distance_diopters"


@dataclass(frozen=True)
class FakeConfig:
    min_zoom: float = DEFAULT_MIN_ZOOM
    max_zoom: float = DEFAULT_MAX_ZOOM
    has_flash_unit: bool = True
    ready: bool = True
    capture_fails: bool = False
    # The app is in the background: camera endpoints return 503 camera_not_ready.
    background: bool = False
    # The physical orientation of the fake phone. The rotation follows it while it is not locked.
    physical_rotation: int = 0
    internal_error: bool = False
    # Seconds after the server start in which the camera endpoints return 503 (bind and start state).
    start_delay: float = 0.0
    snapshot: bytes = TEST_JPEG
    # An app from before POST /v1/preview: the path is unknown (404), and the status has no preview fields.
    no_preview: bool = False
    focus_diopters: float = DEFAULT_FOCUS_DIOPTERS
    # A phone without the vendor in-sensor zoom: POST /v1/camera answers 200 with "unsupported".
    in_sensor_zoom_unsupported: bool = False
    # A phone without the macro autofocus mode: POST /v1/camera af_mode "macro" answers 200, and the mode stays
    # "continuous".
    no_macro: bool = False


class ApiError(Exception):
    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class FakeCamera:
    """In-memory camera. All methods are thread safe."""

    def __init__(self, config: FakeConfig) -> None:
        self.config = config
        self._ready_at = time.monotonic() + config.start_delay
        self._lock = threading.Lock()
        self._status = CameraStatus(
            zoom_ratio=start_zoom(config.min_zoom, config.max_zoom),
            min_zoom_ratio=config.min_zoom,
            max_zoom_ratio=config.max_zoom,
            torch_enabled=False,
            has_flash_unit=config.has_flash_unit,
            rotation_degrees=config.physical_rotation,
            rotation_locked=False,
            focus={
                "distance_diopters": config.focus_diopters,
                "state": FOCUS_STATE,
                "calibration": FOCUS_CALIBRATION,
                "min_distance_diopters": MIN_FOCUS_DIOPTERS,
            },
            optics=dict(OPTICS),
            app_start_id=str(uuid.uuid7()),
            preview_region=dict(PREVIEW_REGION),
        )
        # The last focus point (the request fields) and the end of its scan: tests read them.
        self.last_focus: dict[str, float] | None = None
        self.last_overlay: list[dict[str, Any]] = []
        self.last_arrows: list[dict[str, Any]] = []
        self._scan_until = 0.0

    def _require_ready(self) -> None:
        if self.config.background:
            raise ApiError(ErrorCode.CAMERA_NOT_READY, "Camera is not active")
        if not self.config.ready or time.monotonic() < self._ready_at:
            raise ApiError(ErrorCode.CAMERA_NOT_READY, "Camera is not bound yet")
        if self.config.internal_error:
            raise ApiError(ErrorCode.INTERNAL_ERROR, "Unexpected error (fake)")

    def _clamp(self, ratio: float) -> float:
        return min(max(ratio, self._status.min_zoom_ratio), self._status.max_zoom_ratio)

    def status(self) -> CameraStatus:
        self._require_ready()
        with self._lock:
            status = CameraStatus(**asdict(self._status))
        if status.preview_region is not None and status.rotation_degrees in SIDEWAYS_ROTATIONS:
            status.preview_region = dict(PREVIEW_REGION_SIDEWAYS)
        status.overlay_region = overlay_region(
            status.preview_region,
            status.rotation_degrees,
            status.preview_flip_horizontal,
            status.preview_flip_vertical,
        )
        if status.focus is not None and time.monotonic() < self._scan_until:
            status.focus = {**status.focus, "state": FOCUS_SCANNING}
        return status

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

    def rotation(self, degrees: int | None) -> CameraStatus:
        """`degrees` locks the rotation. None goes back to the physical orientation."""
        self._require_ready()
        with self._lock:
            self._status.rotation_locked = degrees is not None
            self._status.rotation_degrees = self.config.physical_rotation if degrees is None else degrees
        return self.status()

    def camera(self, in_sensor_zoom: bool | None, af_mode: str | None = None) -> CameraStatus:
        """Set the in-sensor zoom (the real app binds the camera again) and/or the autofocus mode. Zoom and torch
        stay."""
        self._require_ready()
        with self._lock:
            if af_mode is not None:
                self._status.af_mode = AF_CONTINUOUS if self.config.no_macro else af_mode
            if in_sensor_zoom is not None and self.config.in_sensor_zoom_unsupported:
                self._status.in_sensor_zoom = IN_SENSOR_ZOOM_UNSUPPORTED
            elif in_sensor_zoom is not None:
                self._status.in_sensor_zoom = IN_SENSOR_ZOOM_ON if in_sensor_zoom else IN_SENSOR_ZOOM_OFF
        return self.status()

    def focus(self, point: dict[str, float]) -> CameraStatus:
        """Focus on a point: "scanning" for FOCUS_SCAN_SECONDS, then "focused"."""
        self._require_ready()
        if "screen_y" in point and not PREVIEW_SCREEN_TOP <= point["screen_y"] <= PREVIEW_SCREEN_BOTTOM:
            raise ApiError(ErrorCode.BAD_REQUEST, OUTSIDE_PREVIEW)
        with self._lock:
            self.last_focus = dict(point)
            self._scan_until = time.monotonic() + FOCUS_SCAN_SECONDS
        return self.status()

    def overlay(self, boxes: list[dict[str, Any]], arrows: list[dict[str, Any]]) -> CameraStatus:
        """Show the boxes and arrows over the preview (the fake keeps their number and the last lists for tests)."""
        self._require_ready()
        with self._lock:
            self.last_overlay = list(boxes)
            self.last_arrows = list(arrows)
            self._status.overlay_boxes = len(boxes)
            self._status.overlay_arrows = len(arrows)
        return self.status()

    def overlay_visible(self, visible: bool) -> CameraStatus:
        """Hide or show the boxes and arrows (they stay)."""
        self._require_ready()
        with self._lock:
            self._status.overlay_visible = visible
        return self.status()

    def move_to(self, diopters: float) -> CameraStatus:
        """Test only: the phone is now at 100 / diopters cm from the board."""
        with self._lock:
            self._status.focus = {**(self._status.focus or {}), FAKE_FOCUS_FIELD: diopters}
        return self.status()

    def preview(self, flip_horizontal: bool, flip_vertical: bool) -> CameraStatus:
        """Mirror the camera preview. The snapshot stays in the true orientation."""
        self._require_ready()
        with self._lock:
            self._status.preview_flip_horizontal = flip_horizontal
            self._status.preview_flip_vertical = flip_vertical
        return self.status()

    def snapshot(self) -> bytes:
        return self.snapshot_with_headers()[0]

    def snapshot_with_headers(self) -> tuple[bytes, dict[str, str]]:
        """The still and the response headers: the rotation that it was taken with, and the app start id."""
        self._require_ready()
        if self.config.capture_fails:
            raise ApiError(ErrorCode.CAPTURE_FAILED, "Still capture failed (fake)")
        with self._lock:
            degrees = self._status.rotation_degrees
            app_start_id = self._status.app_start_id
        # Like CameraX: the still is the sensor image turned clockwise by (sensor orientation - rotation).
        turn = (SENSOR_ORIENTATION - degrees) % FULL_TURN
        headers = {Header.ROTATION_DEGREES: str(degrees)}
        if app_start_id:
            headers[Header.APP_START_ID] = app_start_id
        return turn_jpeg(self.config.snapshot, turn), headers


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


def check_tag(item: dict[str, Any]) -> None:
    """A box or an arrow can have a tag of 1 to 3 ASCII letters or digits."""
    tag = item.get(TAG_FIELD)
    if TAG_FIELD in item and not (isinstance(tag, str) and TAG_PATTERN.fullmatch(tag)):
        raise ApiError(ErrorCode.BAD_REQUEST, f'"{TAG_FIELD}" must match {TAG_PATTERN.pattern}')


def check_overlay_box(box: Any) -> None:
    if not isinstance(box, dict) or set(box) - {TAG_FIELD} != OVERLAY_BOX_FIELDS:
        raise ApiError(ErrorCode.BAD_REQUEST, f"Each box needs exactly {sorted(OVERLAY_BOX_FIELDS)} (and a tag)")
    check_tag(box)
    numbers = [box[name] for name in ("snapshot_x", "snapshot_y", "width", "height")]
    if not all(is_number(value) for value in numbers):
        raise ApiError(ErrorCode.BAD_REQUEST, "Box coordinates must be numbers")
    x, y, width, height = numbers
    inside = x >= 0 and y >= 0 and x + width <= 1 + OVERLAY_TOLERANCE and y + height <= 1 + OVERLAY_TOLERANCE
    if width <= 0 or height <= 0 or not inside:
        raise ApiError(ErrorCode.BAD_REQUEST, "A box needs width and height > 0 and must be inside the image")
    label = box["label"]
    if not isinstance(label, str) or len(label) > OVERLAY_MAX_LABEL:
        raise ApiError(ErrorCode.BAD_REQUEST, f'"label" must be a string of at most {OVERLAY_MAX_LABEL} characters')


def check_overlay_arrow(arrow: Any) -> None:
    if not isinstance(arrow, dict) or set(arrow) - {TAG_FIELD} != OVERLAY_ARROW_FIELDS:
        raise ApiError(ErrorCode.BAD_REQUEST, f"Each arrow needs exactly {sorted(OVERLAY_ARROW_FIELDS)} (and a tag)")
    check_tag(arrow)
    if not is_number(arrow["angle_deg"]):
        raise ApiError(ErrorCode.BAD_REQUEST, '"angle_deg" must be a number')
    label = arrow["label"]
    if not isinstance(label, str) or len(label) > OVERLAY_MAX_LABEL:
        raise ApiError(ErrorCode.BAD_REQUEST, f'"label" must be a string of at most {OVERLAY_MAX_LABEL} characters')


class Handler(BaseHTTPRequestHandler):
    camera: FakeCamera  # set by make_server
    quiet: bool = False

    def log_message(self, format: str, *args: Any) -> None:
        if not self.quiet:
            super().log_message(format, *args)

    def _send(
        self, status: HTTPStatus, content_type: ContentType, body: bytes, headers: dict[str, str] | None = None
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        self._send(status, ContentType.JSON, json.dumps(payload).encode())

    def _send_error(self, err: ApiError) -> None:
        self._send_json({"error": err.code, "message": err.message}, ERROR_STATUS[err.code])

    def _read_body(self) -> bytes:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != ContentType.JSON:
            raise ApiError(ErrorCode.BAD_REQUEST, f"Content-Type must be {ContentType.JSON}")
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            raise ApiError(ErrorCode.BAD_REQUEST, f"Body is larger than {MAX_BODY_BYTES} bytes")
        return self.rfile.read(length)

    def _send_status(self, status: CameraStatus) -> None:
        data = asdict(status)
        if self.camera.config.no_preview:
            for name in NEWER_STATUS_FIELDS:
                data.pop(name)
        self._send_json(data)

    def _dispatch(self, routes: dict[str, Callable[[], None]]) -> None:
        path = self.path.split("?", 1)[0]
        route = routes.get(path)
        known = KNOWN_PATHS - NEWER_PATHS if self.camera.config.no_preview else KNOWN_PATHS
        try:
            if route is None and path in known:
                raise ApiError(ErrorCode.METHOD_NOT_ALLOWED, f"{self.command} is not allowed on {path}")
            if route is None:
                raise ApiError(ErrorCode.NOT_FOUND, f"No route for {self.command} {self.path}")
            route()
        except ApiError as err:
            self._send_error(err)

    def do_GET(self) -> None:
        self._dispatch(
            {
                Route.HEALTH: self.health,
                Route.STATUS: lambda: self._send_status(self.camera.status()),
                Route.SNAPSHOT: self.snapshot,
            }
        )

    def do_POST(self) -> None:
        routes = {Route.ZOOM: self.zoom, Route.TORCH: self.torch, Route.ROTATION: self.rotation}
        if not self.camera.config.no_preview:
            routes[Route.PREVIEW] = self.preview
            routes[Route.CAMERA] = self.camera_settings
            routes[Route.FOCUS] = self.focus
            routes[Route.OVERLAY] = self.overlay
        routes[Route.FAKE_FOCUS] = self.fake_focus
        self._dispatch(routes)

    def do_PUT(self) -> None:
        self._dispatch({})

    def do_DELETE(self) -> None:
        self._dispatch({})

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
        self._send_status(status)

    def torch(self) -> None:
        data = parse_body(self._read_body())
        enabled = data.get("enabled")
        if not isinstance(enabled, bool):
            raise ApiError(ErrorCode.BAD_REQUEST, '"enabled" must be true or false')
        self._send_status(self.camera.torch(enabled))

    def rotation(self) -> None:
        data = parse_body(self._read_body())
        has_degrees, has_auto = "degrees" in data, "auto" in data
        if has_degrees == has_auto:
            raise ApiError(ErrorCode.BAD_REQUEST, 'Send exactly one of "degrees" or "auto"')
        if has_auto:
            if data["auto"] is not True:
                raise ApiError(ErrorCode.BAD_REQUEST, '"auto" must be true')
            self._send_status(self.camera.rotation(None))
            return
        degrees = data["degrees"]
        if isinstance(degrees, bool) or not isinstance(degrees, int) or degrees not in ROTATIONS:
            raise ApiError(ErrorCode.BAD_REQUEST, f'"degrees" must be one of {list(ROTATIONS)}')
        self._send_status(self.camera.rotation(degrees))

    def camera_settings(self) -> None:
        data = parse_body(self._read_body())
        if not data or not set(data) <= CAMERA_FIELDS:
            raise ApiError(ErrorCode.BAD_REQUEST, f'Send "{CAMERA_FIELD}" and/or "{AF_MODE_FIELD}", nothing else')
        zoom, af_mode = data.get(CAMERA_FIELD), data.get(AF_MODE_FIELD)
        if CAMERA_FIELD in data and not isinstance(zoom, bool):
            raise ApiError(ErrorCode.BAD_REQUEST, f'"{CAMERA_FIELD}" must be true or false')
        if AF_MODE_FIELD in data and af_mode not in AF_MODES:
            raise ApiError(ErrorCode.BAD_REQUEST, f'"{AF_MODE_FIELD}" must be one of {sorted(AF_MODES)}')
        self._send_status(self.camera.camera(zoom, af_mode))

    def focus(self) -> None:
        data = parse_body(self._read_body())
        if set(data) not in (SCREEN_FOCUS_FIELDS, SNAPSHOT_FOCUS_FIELDS):
            raise ApiError(ErrorCode.BAD_REQUEST, FOCUS_FIELDS_MESSAGE)
        if not all(is_number(value) and 0 <= value <= 1 for value in data.values()):
            raise ApiError(ErrorCode.BAD_REQUEST, "Each focus coordinate must be a number from 0 to 1")
        self._send_status(self.camera.focus({name: float(value) for name, value in data.items()}))

    def overlay(self) -> None:
        data = parse_body(self._read_body())
        if VISIBLE_FIELD in data:
            # Hide or show the boxes and arrows, and keep them. A body with `visible` has nothing else.
            if set(data) != {VISIBLE_FIELD} or not isinstance(data[VISIBLE_FIELD], bool):
                raise ApiError(ErrorCode.BAD_REQUEST, f'Send "{VISIBLE_FIELD}" alone: true or false')
            self._send_status(self.camera.overlay_visible(data[VISIBLE_FIELD]))
            return
        boxes, arrows = data.get("boxes"), data.get("arrows", [])
        if not set(data) <= {"boxes", "arrows"} or not isinstance(boxes, list) or not isinstance(arrows, list):
            raise ApiError(ErrorCode.BAD_REQUEST, 'Send "boxes": a list, and optionally "arrows": a list')
        if len(boxes) > OVERLAY_MAX_BOXES:
            raise ApiError(ErrorCode.BAD_REQUEST, f"At most {OVERLAY_MAX_BOXES} boxes")
        if len(arrows) > OVERLAY_MAX_ARROWS:
            raise ApiError(ErrorCode.BAD_REQUEST, f"At most {OVERLAY_MAX_ARROWS} arrows")
        for box in boxes:
            check_overlay_box(box)
        for arrow in arrows:
            check_overlay_arrow(arrow)
        self._send_status(self.camera.overlay(boxes, arrows))

    def fake_focus(self) -> None:
        data = parse_body(self._read_body())
        diopters = data.get(FAKE_FOCUS_FIELD)
        if not is_number(diopters) or diopters < 0:
            raise ApiError(ErrorCode.BAD_REQUEST, f'"{FAKE_FOCUS_FIELD}" must be a number >= 0')
        self._send_status(self.camera.move_to(float(diopters)))

    def preview(self) -> None:
        data = parse_body(self._read_body())
        if set(data) != PREVIEW_FIELDS:
            raise ApiError(ErrorCode.BAD_REQUEST, 'Send exactly "flip_horizontal" and "flip_vertical"')
        if not all(isinstance(data[name], bool) for name in PREVIEW_FIELDS):
            raise ApiError(ErrorCode.BAD_REQUEST, '"flip_horizontal" and "flip_vertical" must be true or false')
        self._send_status(self.camera.preview(data["flip_horizontal"], data["flip_vertical"]))

    def snapshot(self) -> None:
        jpeg, headers = self.camera.snapshot_with_headers()
        self._send(HTTPStatus.OK, ContentType.JPEG, jpeg, headers)


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
        ("ultrawide", FakeConfig(min_zoom=ULTRAWIDE_MIN_ZOOM), qa_contract.Expect.READY, []),
        ("no flash", FakeConfig(has_flash_unit=False), qa_contract.Expect.READY, []),
        ("not ready", FakeConfig(ready=False), qa_contract.Expect.NOT_READY, []),
        (
            "capture fails",
            FakeConfig(capture_fails=True),
            qa_contract.Expect.READY,
            [qa_contract.check_capture_failed],
        ),
        ("starting", FakeConfig(start_delay=SELF_CHECK_START_DELAY), qa_contract.Expect.STARTING, []),
        ("starting race", FakeConfig(start_delay=SELF_CHECK_START_DELAY), qa_contract.Expect.STARTING_RACE, []),
        ("internal error", FakeConfig(internal_error=True), qa_contract.Expect.READY, []),
        ("background", FakeConfig(background=True), qa_contract.Expect.BACKGROUND, []),
        ("lying at 270", FakeConfig(physical_rotation=270), qa_contract.Expect.READY, []),
    ]
    all_passed = True
    for label, config, expect_state, extra in scenarios:
        server = make_server(DEFAULT_HOST, EPHEMERAL_PORT, config, quiet=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://{DEFAULT_HOST}:{server.server_address[1]}"
            if label == "internal error":
                # Every camera endpoint fails in this mode, so only these checks apply.
                ctx = qa_contract.Context(
                    qa_contract.Client(base_url, qa_contract.DEFAULT_TIMEOUT_SECONDS),
                    qa_contract.DEFAULT_SNAPSHOT_TIMEOUT_SECONDS,
                    strict=True,
                )
                checks = [
                    qa_contract.check_health,
                    qa_contract.check_internal_error,
                    qa_contract.check_method_not_allowed,
                    qa_contract.check_not_found,
                ]
                all_passed &= qa_contract.print_results(qa_contract.run_checks(ctx, checks), label)
                continue
            results = qa_contract.run_contract(
                base_url,
                qa_contract.Options(
                    expect_state=expect_state, strict=True, after_start=expect_state is qa_contract.Expect.READY
                ),
                extra,
            )
            if label == "capture fails":
                # The normal snapshot check must fail in this mode; the extra check covers it.
                results = [
                    r
                    for r in results
                    if r.name not in {"snapshot", "snapshot_keeps_torch", "snapshot_rotation", "preview_keeps_snapshot"}
                ]
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
    parser.add_argument("--background", action="store_true", help="the app is in the background: camera endpoints 503")
    parser.add_argument(
        "--physical-rotation", type=int, choices=[0, 90, 180, 270], default=0, help="phone orientation for auto"
    )
    parser.add_argument("--internal-error", action="store_true", help="camera endpoints return 500 internal_error")
    parser.add_argument(
        "--no-macro",
        action="store_true",
        help='a phone without the macro autofocus mode: af_mode "macro" stays "continuous"',
    )
    parser.add_argument(
        "--in-sensor-zoom-unsupported",
        action="store_true",
        help='a phone without the vendor in-sensor zoom: POST /v1/camera gives "unsupported"',
    )
    parser.add_argument(
        "--focus-diopters",
        type=float,
        default=DEFAULT_FOCUS_DIOPTERS,
        help='focus distance in diopters (100 / cm); also POST /fake/focus {"distance_diopters": x}',
    )
    parser.add_argument(
        "--no-preview", action="store_true", help="an old app: no POST /v1/preview (404), no preview fields"
    )
    parser.add_argument(
        "--start-delay",
        type=float,
        default=0.0,
        help="seconds after the start in which camera endpoints return 503, like the app before the start state",
    )
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
        internal_error=args.internal_error,
        no_preview=args.no_preview,
        focus_diopters=args.focus_diopters,
        in_sensor_zoom_unsupported=args.in_sensor_zoom_unsupported,
        no_macro=args.no_macro,
        background=args.background,
        physical_rotation=args.physical_rotation,
        start_delay=args.start_delay,
        snapshot=Path(args.snapshot).read_bytes() if args.snapshot else TEST_JPEG,
    )
    if args.snapshot and importlib.util.find_spec(PILLOW_MODULE) is None:
        print("note: Pillow is missing, so the --snapshot file is not turned with the rotation", file=sys.stderr)
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
