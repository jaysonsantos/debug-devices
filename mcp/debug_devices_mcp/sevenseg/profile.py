"""The meter profile: where the LCD is in the webcam crop, and where each segment and symbol is on the LCD.

Coordinates: the corners are pixels of the webcam crop. Every region is normalized: a digit box, the decimal points,
the sign, and the symbols in LCD coordinates (0..1 across the warped LCD), and the 7 segments in digit-box coordinates
(0..1 across one digit box, before the slant). The profile is a JSON file in the state folder (not in git): edit it by
hand to move a region.
"""

from enum import StrEnum
from pathlib import Path

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from debug_devices_mcp.sevenseg.constants import PROFILE_FILE_NAME, STATE_SUBDIR, warp
from debug_devices_mcp.ui.settings import state_dir

PROFILE_VERSION = 1


class Segment(StrEnum):
    """The 7 segments of a digit: a top, b top right, c bottom right, d bottom, e bottom left, f top left, g middle."""

    A = "a"
    B = "b"
    C = "c"
    D = "d"
    E = "e"
    F = "f"
    G = "g"


class Symbol(StrEnum):
    """The fixed LCD symbols that the decoder reads. A template lists the ones that its meter has."""

    AUTO = "auto"
    HOLD = "hold"
    AC = "ac"
    DC = "dc"
    DIODE = "diode"
    CONTINUITY = "continuity"
    LOW_BATTERY = "low_battery"
    REL = "rel"
    NANO = "nano"
    MICRO = "micro"
    MILLI = "milli"
    KILO = "kilo"
    MEGA = "mega"
    VOLT = "volt"
    AMPERE = "ampere"
    OHM = "ohm"
    FARAD = "farad"
    HERTZ = "hertz"
    PERCENT = "percent"
    CELSIUS = "celsius"
    FAHRENHEIT = "fahrenheit"


class Point(BaseModel):
    model_config = ConfigDict(frozen=True)

    x: float
    y: float


class Rect(BaseModel):
    """A normalized rectangle in its parent frame: top-left corner, width, and height."""

    model_config = ConfigDict(frozen=True)

    x: float
    y: float
    width: float = Field(gt=0)
    height: float = Field(gt=0)

    def moved(self, dx: float, dy: float) -> Rect:
        return self.model_copy(update={"x": self.x + dx, "y": self.y + dy})

    def scaled(self, factor: float, center: Point) -> Rect:
        """Scale around `center` (in the same frame)."""
        return Rect(
            x=center.x + (self.x - center.x) * factor,
            y=center.y + (self.y - center.y) * factor,
            width=self.width * factor,
            height=self.height * factor,
        )


class LcdLayout(BaseModel):
    """The regions of one LCD in normalized coordinates."""

    # Width / height of the LCD: the warp keeps this aspect ratio.
    aspect: float = Field(ge=warp.MIN_ASPECT, le=warp.MAX_ASPECT)
    # The digit boxes from left to right (LCD coordinates). A box is the upright box at the bottom of the digit.
    digits: list[Rect]
    # The slant of the digits: the top of a digit is this part of the box width to the right of its bottom.
    slant: float = 0.0
    # The 7 segments in digit-box coordinates (shared by every digit).
    segments: dict[Segment, Rect]
    # points[i] is the decimal point after digit i (LCD coordinates); the last digit has none.
    points: list[Rect]
    # The minus sign left of the digits.
    sign: Rect
    symbols: dict[Symbol, Rect]


class MeterProfile(BaseModel):
    """One calibrated meter at one webcam crop."""

    version: int = PROFILE_VERSION
    template: str
    # The size of the crop image at calibration: a frame of another size means that the crop changed.
    image_width: int = Field(gt=0)
    image_height: int = Field(gt=0)
    # The LCD corners in crop pixels: top left, top right, bottom right, bottom left.
    corners: list[Point] = Field(min_length=4, max_length=4)
    layout: LcdLayout
    calibrated_at: AwareDatetime


class ProfileError(Exception):
    """The profile file is missing or not valid."""


def default_dir() -> Path:
    """`<state folder>/sevenseg`: the profile and the dataset. Git never sees it."""
    return state_dir() / STATE_SUBDIR


def default_profile_path() -> Path:
    return default_dir() / PROFILE_FILE_NAME


def save_profile(profile: MeterProfile, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(profile.model_dump_json(indent=2) + "\n")
    temporary.replace(path)


def load_profile(path: Path) -> MeterProfile:
    try:
        return MeterProfile.model_validate_json(path.read_bytes())
    except FileNotFoundError as exc:
        raise ProfileError(f"no meter profile at {path}: run `debug-devices-sevenseg calibrate` first") from exc
    except ValueError as exc:
        raise ProfileError(f"the meter profile {path} is not valid: {exc}") from exc
