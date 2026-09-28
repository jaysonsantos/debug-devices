"""Compare mode: the local decoder reads the same frames as the vision model, and the result says if they agree.

The local reading never changes the vision result, its status, its confidence, or the bench gate: `compare_local`
returns a copy of the result with only `local_reading` and `local_agrees` set.
"""

import asyncio
import logging
from collections.abc import Sequence
from enum import StrEnum
from functools import cache
from pathlib import Path

from pydantic import BaseModel

from debug_devices_mcp.multimeter import (
    MINUS_SIGNS,
    MeterResult,
    MeterSource,
    MultimeterReading,
    UnitFamily,
    is_overload,
    signature,
    unit_parts,
)
from debug_devices_mcp.sevenseg.constants import DATASET_DIR_NAME, PROFILE_FILE_NAME
from debug_devices_mcp.sevenseg.dataset import Dataset, VisionFields, new_entry
from debug_devices_mcp.sevenseg.decode import OVERLOAD_TEXT, DecodedFrame, decode_jpeg, unreadable
from debug_devices_mcp.sevenseg.profile import MeterProfile, ProfileError, default_dir, load_profile
from debug_devices_mcp.sevenseg.reading import LocalReading, LocalStatus
from debug_devices_mcp.sevenseg.stability import combine_readings
from debug_devices_mcp.ui.events import current_call

logger = logging.getLogger(__name__)

# The key of the short line in the monitor log entry (ToolCall details).
LOG_DETAIL_KEY = "local_decoder"
MILLISECONDS_PER_SECOND = 1000

# region: agreement


class Field(StrEnum):
    READABLE = "readable"
    DIGITS = "digits"
    POINT = "point"
    SIGN = "sign"
    UNIT = "unit"
    MODE = "mode"


COMPARED_FIELDS = (Field.DIGITS, Field.POINT, Field.SIGN, Field.UNIT, Field.MODE)
type FieldValues = dict[Field, tuple[str, str]]


def vision_negative(display_text: str) -> bool:
    """A minus before the first digit, as in multimeter.signed_value."""
    first = next((index for index, char in enumerate(display_text) if char.isdigit()), len(display_text))
    return any(char in MINUS_SIGNS for char in display_text[:first])


def unit_text(unit: str) -> str:
    """The unit as family and prefix, so that "kohm", "kΩ", and "kΩ" are the same."""
    family, prefix = unit_parts(unit)
    return f"{prefix}{family}" if family is not UnitFamily.UNKNOWN else "unknown"


def field_values(vision: VisionFields, local: LocalReading) -> FieldValues:
    """The compared fields as text: (vision, local)."""
    values: FieldValues = {Field.READABLE: (str(vision.readable), str(local.readable))}
    if not (vision.readable and local.readable):
        return values
    if is_overload(vision.display_text):
        vision_digits, vision_point = OVERLOAD_TEXT, None
    else:
        vision_digits, vision_point = signature(vision.display_text)
    local_digits = OVERLOAD_TEXT if local.overload else (local.digits or "")
    values[Field.DIGITS] = (vision_digits, local_digits)
    values[Field.POINT] = (str(vision_point), str(local.digits_before_point))
    values[Field.SIGN] = (str(vision_negative(vision.display_text)), str(local.negative))
    values[Field.UNIT] = (unit_text(vision.unit), unit_text(local.unit))
    values[Field.MODE] = (str(vision.mode), str(local.mode))
    return values


def differences(vision: VisionFields, local: LocalReading) -> list[Field]:
    return [name for name, (seen, read) in field_values(vision, local).items() if seen != read]


def agrees(vision: VisionFields, local: LocalReading) -> bool:
    """Both are readable and every compared field is the same."""
    return vision.readable and local.readable and not differences(vision, local)


# endregion: agreement

# region: compare


class Comparison(BaseModel):
    local_reading: LocalReading
    local_agrees: bool
    differences: list[Field]
    log_line: str


def log_line(local: LocalReading, differ: list[Field], elapsed_seconds: float) -> str:
    shown = f"{local.display_text or '-'} {local.unit} {local.mode}" if local.readable else "unreadable"
    verdict = "agrees" if not differ else "differs in " + ", ".join(differ)
    return f"local: {shown} ({local.status}), {verdict}, {elapsed_seconds * MILLISECONDS_PER_SECOND:.0f} ms"


class LocalMeter:
    """The local decoder with the profile and the dataset in one folder (the state folder by default)."""

    def __init__(self, directory: Path, save_frames: bool = True) -> None:
        self.profile_path = directory / PROFILE_FILE_NAME
        self.dataset = Dataset(directory / DATASET_DIR_NAME)
        self.save_frames = save_frames
        self._cached: tuple[int, MeterProfile] | None = None

    def profile(self) -> MeterProfile:
        """The profile, read again when the file changed (the user can edit it while the server runs)."""
        try:
            stamp = self.profile_path.stat().st_mtime_ns
        except FileNotFoundError:
            stamp = -1
        if self._cached is None or self._cached[0] != stamp:
            self._cached = (stamp, load_profile(self.profile_path))
        return self._cached[1]

    def compare(self, result: MeterResult, jpegs: Sequence[bytes], readings: Sequence[MultimeterReading]) -> Comparison:
        """Decode each frame, combine them (every frame must agree, as for the vision result), and compare with the
        combined vision result and each vision frame."""
        try:
            profile = self.profile()
        except ProfileError as exc:
            local = unreadable(str(exc)).model_copy(update={"status": LocalStatus.NO_PROFILE})
            return Comparison(
                local_reading=local, local_agrees=False, differences=[], log_line=log_line(local, [], 0.0)
            )
        decoded: list[DecodedFrame] = [decode_jpeg(jpeg, profile) for jpeg in jpegs]
        local = combine_readings([frame.reading for frame in decoded], min_agree=len(decoded))
        # Every vision frame must agree too: frames that disagree ("5.10", then "51.0") are not one vision reading.
        visions = [VisionFields.of_result(result), *(VisionFields.of_reading(reading) for reading in readings)]
        differ = list(dict.fromkeys(name for seen in visions for name in differences(seen, local)))
        agreed = all(agrees(seen, local) for seen in visions)
        if self.save_frames:
            self._save(result, jpegs, readings, decoded, profile)
        elapsed = sum(frame.elapsed.total_seconds() for frame in decoded)
        return Comparison(
            local_reading=local, local_agrees=agreed, differences=differ, log_line=log_line(local, differ, elapsed)
        )

    def _save(
        self,
        result: MeterResult,
        jpegs: Sequence[bytes],
        readings: Sequence[MultimeterReading],
        decoded: Sequence[DecodedFrame],
        profile: MeterProfile,
    ) -> None:
        frames = [*result.frames, *[None] * len(jpegs)][: len(jpegs)]
        for jpeg, reading, frame, capture in zip(jpegs, readings, decoded, frames, strict=True):
            entry = new_entry(
                VisionFields.of_reading(reading), frame.reading, frame.regions, capture, profile.calibrated_at
            )
            self.dataset.add(entry, jpeg)


@cache
def meter_in(directory: Path) -> LocalMeter:
    return LocalMeter(directory)


def default_meter() -> LocalMeter:
    """The meter of the current state folder (one per folder: the profile cache stays between calls)."""
    return meter_in(default_dir())


async def compare_local(
    result: MeterResult,
    source: MeterSource,
    jpegs: Sequence[bytes],
    readings: Sequence[MultimeterReading],
    meter: LocalMeter | None = None,
) -> MeterResult:
    """The result with `local_reading` and `local_agrees` added, and the short line in the monitor log entry.

    Only the webcam: the profile is for the webcam crop. Any local error leaves the result as it is.
    """
    if source is not MeterSource.WEBCAM:
        return result
    meter = meter or default_meter()
    try:
        comparison = await asyncio.to_thread(meter.compare, result, jpegs, readings)
    except Exception:
        # Compare mode is for evaluation: a local failure must never fail multimeter_read or bench_measure.
        logger.exception("the local meter decoder failed")
        return result
    call = current_call()
    if call is not None:
        call.set_detail(LOG_DETAIL_KEY, comparison.log_line)
    return result.model_copy(
        update={"local_reading": comparison.local_reading, "local_agrees": comparison.local_agrees}
    )


# endregion: compare
