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
from debug_devices_mcp.sevenseg.decode import (
    OVERLOAD_TEXT,
    DecodedFrame,
    Gray,
    decode_with_lcd,
    read_image,
    unreadable,
)
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


NOT_COMPARED = "not compared"
UNREADABLE_TEXT = "unreadable"
FAILED_PROBLEM = "the local decoder failed: {error}"
# One meter frame: the JPEG that the vision model read, and its reading.
type MeterFrame = tuple[bytes, MultimeterReading]
# Why a dataset entry has no image. The dataset never keeps a webcam frame.
NO_CROP_NOTE = "no image saved: no webcam crop is set, so the frame can show more than the meter"
MISMATCH_NOTE = "no image saved: the frame does not match the profile (the webcam crop changed)"


def log_line(local: LocalReading, agreed: bool, differ: list[Field], elapsed_seconds: float) -> str:
    unreadable_reason = local.problems[0] if local.problems else UNREADABLE_TEXT
    shown = f"{local.display_text or '-'} {local.unit} {local.mode}" if local.readable else unreadable_reason
    verdict = "agrees" if agreed else ("differs in " + ", ".join(differ) if differ else NOT_COMPARED)
    return f"local: {shown} ({local.status}), {verdict}, {elapsed_seconds * MILLISECONDS_PER_SECOND:.0f} ms"


def failed(error: Exception) -> Comparison:
    """The comparison when the local code raised: an unreadable local reading that names the error."""
    local = unreadable(FAILED_PROBLEM.format(error=f"{type(error).__name__}: {error}"))
    return Comparison(local_reading=local, local_agrees=False, differences=[], log_line=log_line(local, False, [], 0.0))


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

    def compare(self, result: MeterResult, frames: Sequence[MeterFrame], crop_set: bool) -> Comparison:
        """Decode each frame, combine them (every frame must agree, as for the vision result), and compare with the
        combined vision result and each vision frame.

        `crop_set`: a webcam crop is set. Without it, the dataset gets no image (the frame can show people).
        """
        try:
            profile = self.profile()
        except ProfileError as exc:
            local = unreadable(str(exc)).model_copy(update={"status": LocalStatus.NO_PROFILE})
            return Comparison(
                local_reading=local, local_agrees=False, differences=[], log_line=log_line(local, False, [], 0.0)
            )
        readings = [reading for _, reading in frames]
        decoded_frames = [decode_with_lcd(read_image(jpeg), profile) for jpeg, _ in frames]
        decoded = [frame for frame, _ in decoded_frames]
        local = combine_readings([frame.reading for frame in decoded], min_agree=len(decoded))
        # Every vision frame must agree too: frames that disagree ("5.10", then "51.0") are not one vision reading.
        visions = [VisionFields.of_result(result), *(VisionFields.of_reading(reading) for reading in readings)]
        differ = list(dict.fromkeys(name for seen in visions for name in differences(seen, local)))
        agreed = all(agrees(seen, local) for seen in visions)
        if self.save_frames:
            self._save(result, readings, decoded_frames, profile, crop_set)
        elapsed = sum(frame.elapsed.total_seconds() for frame in decoded)
        return Comparison(
            local_reading=local,
            local_agrees=agreed,
            differences=differ,
            log_line=log_line(local, agreed, differ, elapsed),
        )

    def _save(
        self,
        result: MeterResult,
        readings: Sequence[MultimeterReading],
        frames: Sequence[tuple[DecodedFrame, Gray | None]],
        profile: MeterProfile,
        crop_set: bool,
    ) -> None:
        """One entry per frame: the warped LCD image only when a crop is set and the frame matches the profile."""
        captures = [*result.frames, *[None] * len(frames)][: len(frames)]
        for reading, (frame, lcd), capture in zip(readings, frames, captures, strict=True):
            entry = new_entry(
                VisionFields.of_reading(reading), frame.reading, frame.regions, capture, profile.calibrated_at
            )
            if not crop_set:
                self.dataset.add(entry, None, NO_CROP_NOTE)
            elif lcd is None:
                self.dataset.add(entry, None, MISMATCH_NOTE)
            else:
                self.dataset.add(entry, lcd)


@cache
def meter_in(directory: Path) -> LocalMeter:
    return LocalMeter(directory)


def default_meter() -> LocalMeter:
    """The meter of the current state folder (one per folder: the profile cache stays between calls)."""
    return meter_in(default_dir())


async def compare_local(
    result: MeterResult,
    source: MeterSource,
    frames: Sequence[MeterFrame],
    *,
    crop_set: bool,
    meter: LocalMeter | None = None,
) -> MeterResult:
    """The result with `local_reading` and `local_agrees` added, and the short line in the monitor log entry.

    Only the webcam: the profile is for the webcam crop. Any local error leaves the result as it is.
    """
    if source is not MeterSource.WEBCAM:
        return result
    meter = meter or default_meter()
    try:
        comparison = await asyncio.to_thread(meter.compare, result, frames, crop_set)
    except Exception as exc:
        # Compare mode is for evaluation: a local failure must never fail multimeter_read or bench_measure. The
        # local fields and the log line say that it failed, so it does not look like off mode.
        logger.exception("the local meter decoder failed")
        comparison = failed(exc)
    call = current_call()
    if call is not None:
        call.set_detail(LOG_DETAIL_KEY, comparison.log_line)
    return result.model_copy(
        update={"local_reading": comparison.local_reading, "local_agrees": comparison.local_agrees}
    )


# endregion: compare
