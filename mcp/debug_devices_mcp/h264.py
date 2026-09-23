"""H.264 Annex B: split a byte stream into NAL units and access units, and read the codec string from the SPS."""

from dataclasses import dataclass
from enum import IntEnum

START_CODE = b"\x00\x00\x01"
LONG_START_CODE = b"\x00\x00\x00\x01"
EMULATION_PREVENTION = b"\x00\x00\x03"
NAL_TYPE_MASK = 0x1F
# The first bit of a slice header is the ue(v) `first_mb_in_slice`. A set bit means the value 0: a new picture.
FIRST_MB_ZERO_MASK = 0x80
CODEC_PREFIX = "avc1."
SPS_CODEC_BYTES = 3


class NalType(IntEnum):
    SLICE = 1
    IDR = 5
    SEI = 6
    SPS = 7
    PPS = 8
    AUD = 9


VCL_TYPES = frozenset({NalType.SLICE, NalType.IDR})
# Non-VCL units that start a new access unit when they come after a picture.
AU_START_TYPES = frozenset({NalType.SEI, NalType.SPS, NalType.PPS, NalType.AUD})


def nal_type(nal: bytes) -> int:
    return nal[0] & NAL_TYPE_MASK


def remove_emulation_prevention(data: bytes) -> bytes:
    """`00 00 03` becomes `00 00`: the RBSP of a NAL unit."""
    return data.replace(EMULATION_PREVENTION, START_CODE[:2])


def codec_string(sps: bytes) -> str:
    """The WebCodecs codec string `avc1.PPCCLL` from an SPS NAL unit: profile, constraint flags, level."""
    if len(sps) <= SPS_CODEC_BYTES or nal_type(sps) != NalType.SPS:
        raise ValueError("not an SPS NAL unit")
    profile, constraints, level = remove_emulation_prevention(sps[1:])[:SPS_CODEC_BYTES]
    return f"{CODEC_PREFIX}{profile:02x}{constraints:02x}{level:02x}"


class AnnexBParser:
    """Feed bytes, get complete NAL units (without start codes). A unit is complete when the next one starts."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        # Start of the current unit (after its start code), or None before the first start code.
        self._unit_start: int | None = None
        self._scan = 0

    def feed(self, data: bytes) -> list[bytes]:
        self._buffer += data
        units: list[bytes] = []
        while (found := self._buffer.find(START_CODE, self._scan)) != -1:
            if self._unit_start is not None:
                unit = bytes(self._buffer[self._unit_start : found]).rstrip(b"\x00")
                if unit:
                    units.append(unit)
            self._unit_start = found + len(START_CODE)
            self._scan = self._unit_start
        # Keep the last two bytes in the scan: a start code can cross two chunks.
        self._scan = max(self._unit_start or 0, len(self._buffer) - len(START_CODE) + 1)
        if self._unit_start is not None and self._unit_start > 0:
            del self._buffer[: self._unit_start]
            self._scan -= self._unit_start
            self._unit_start = 0
        return units

    def flush(self) -> list[bytes]:
        """The last unit, at the end of the stream."""
        if self._unit_start is None:
            return []
        unit = bytes(self._buffer[self._unit_start :]).rstrip(b"\x00")
        self._buffer.clear()
        self._unit_start = None
        self._scan = 0
        return [unit] if unit else []


@dataclass(frozen=True)
class AccessUnit:
    """One picture in Annex B form. A key unit always starts with the SPS and PPS, so it decodes alone."""

    data: bytes
    key: bool
    codec: str | None


def annex_b(units: list[bytes]) -> bytes:
    return b"".join(LONG_START_CODE + unit for unit in units)


class AccessUnitAssembler:
    """Group NAL units into access units. It keeps the last SPS and PPS for key frames that come without them."""

    def __init__(self) -> None:
        self._pending: list[bytes] = []
        self._has_picture = False
        self.sps: bytes | None = None
        self.pps: bytes | None = None

    def feed(self, unit: bytes) -> AccessUnit | None:
        kind = nal_type(unit)
        starts_picture = kind in VCL_TYPES and len(unit) > 1 and bool(unit[1] & FIRST_MB_ZERO_MASK)
        complete = None
        if self._has_picture and (kind in AU_START_TYPES or starts_picture):
            complete = self.flush()
        if kind == NalType.SPS:
            self.sps = unit
        elif kind == NalType.PPS:
            self.pps = unit
        self._pending.append(unit)
        self._has_picture = self._has_picture or kind in VCL_TYPES
        return complete

    def flush(self) -> AccessUnit | None:
        units, self._pending, self._has_picture = self._pending, [], False
        if not any(nal_type(unit) in VCL_TYPES for unit in units):
            return None
        key = any(nal_type(unit) == NalType.IDR for unit in units)
        if key and self.sps is not None and self.pps is not None:
            units = [self.sps, self.pps, *(unit for unit in units if nal_type(unit) not in {NalType.SPS, NalType.PPS})]
        return AccessUnit(data=annex_b(units), key=key, codec=codec_string(self.sps) if self.sps else None)
