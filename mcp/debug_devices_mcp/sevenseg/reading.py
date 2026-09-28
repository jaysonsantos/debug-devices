"""What the local decoder read: the same fields as the vision reading where possible (MultimeterReading)."""

from enum import StrEnum

from pydantic import BaseModel

from debug_devices_mcp.meter_mode import MeterMode


class LocalStatus(StrEnum):
    # Every region is clear, the digits and the unit are known, and (with several frames) the frames agree.
    READ = "read"
    # A region is unclear, a digit pattern is unknown, two points are on, or the unit symbol is missing.
    UNCERTAIN = "uncertain"
    # The readable frames do not agree on the digits, the point, the sign, the unit, or the mode.
    UNSTABLE = "unstable"
    # The LCD contrast is too low, the frame does not fit the profile (the crop changed), too few frames are
    # readable, or the local decoder failed.
    UNREADABLE = "unreadable"
    # No valid profile (missing, or a file that does not validate): run `debug-devices-sevenseg calibrate`.
    NO_PROFILE = "no_profile"


class LocalReading(BaseModel):
    """The local decoder result. It is never a measurement: only multimeter_read's vision result is one."""

    status: LocalStatus
    readable: bool
    # The LCD text as decoded, for example "-5.10" or "OL". Leading blank digits are not in it.
    display_text: str
    # The digits without the sign and the point ("510" for "5.10"); None for an overload or no digits.
    digits: str | None
    # How many digits are left of the decimal point; None when no point is on.
    digits_before_point: int | None
    negative: bool
    value: float | None
    # The unit with its prefix, for example "mV" or "kΩ"; "unknown" when no unit symbol is clearly on.
    unit: str
    mode: MeterMode
    flags: list[str]
    overload: bool
    # 0..1: the weakest region that decides the value, the unit, or the mode.
    confidence: float
    # The LCD contrast of the frame (0..1): the darkness of the segments against the background.
    contrast: float
    # Regions near the on/off threshold, for example "digit2.g" or "point1".
    weak_regions: list[str]
    problems: list[str]
    # With several frames: how many were decoded and how many agree with the reported one.
    frames: int = 1
    agreeing_frames: int = 1
