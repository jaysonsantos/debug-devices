"""Models of the `obv-dump` JSON in docs/boardview-json.md. Coordinates are integers in mil."""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel


class BoardFormat(StrEnum):
    BRD = "brd"
    BRD2 = "brd2"
    BDV = "bdv"
    ASC = "asc"
    BVR = "bvr"
    BVR3 = "bvr3"
    CAD = "cad"
    CST = "cst"
    FZ = "fz"
    CAE = "cae"
    GENCAD = "gencad"
    AD = "ad"
    XZZ = "xzz"


class Side(StrEnum):
    TOP = "top"
    BOTTOM = "bottom"
    BOTH = "both"


class Mounting(StrEnum):
    SMD = "smd"
    THROUGH_HOLE = "through_hole"


class DumpErrorCode(StrEnum):
    UNKNOWN_FORMAT = "unknown_format"
    PARSE_FAILED = "parse_failed"
    KEY_REQUIRED = "key_required"
    KEY_INVALID = "key_invalid"
    IO_ERROR = "io_error"


class MilPoint(BaseModel):
    x: int
    y: int


class MilSegment(BaseModel):
    a: MilPoint
    b: MilPoint


class DumpSource(BaseModel):
    path: str
    format: BoardFormat
    obv_version: str


class DumpPart(BaseModel):
    name: str
    side: Side
    mounting: Mounting
    p1: MilPoint | None
    p2: MilPoint | None
    rotation_deg: float | None
    mfgcode: str
    first_pin: int
    pin_count: int


class DumpPin(BaseModel):
    part: str
    number: str
    name: str
    net: str
    x: int
    y: int
    side: Side
    radius: float
    probe: int


class DumpNail(BaseModel):
    net: str
    x: int
    y: int
    side: Side
    probe: int


class BoardDump(BaseModel):
    schema_version: Literal[1]
    source: DumpSource
    units: Literal["mil"]
    outline: list[MilPoint]
    outline_segments: list[MilSegment]
    parts: list[DumpPart]
    pins: list[DumpPin]
    nails: list[DumpNail]


class DumpError(BaseModel):
    schema_version: Literal[1]
    error: DumpErrorCode | str
    message: str
    # obv-dump 0.1.0 sends null when it does not know the format (the contract says ""). Accept both.
    format: str | None = None
