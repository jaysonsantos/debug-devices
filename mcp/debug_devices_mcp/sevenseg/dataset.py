"""The compare-mode dataset: each meter frame with the vision reading and the local reading (local, git-ignored).

It lives in the state folder, never in the repo. It never keeps a webcam frame: an entry has the warped LCD image
only (the LCD area of the profile), and only when a webcam crop is set and the frame matches the profile. Otherwise
the entry has the text results and a note. At most `max_entries` entries stay: the oldest go first (the ids are
UUID v7, so the name order is the time order). A prune also removes webcam frames of an older version and images
without an entry.

Only files with a UUID v7 entry name (`<uuid>.json`, `<uuid>.lcd.png`, `<uuid>.jpg`) belong to the dataset. The
dataset never reads, counts, or deletes any other file, so a wrong `--dataset` folder loses nothing.
"""

import logging
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

import cv2
from pydantic import AwareDatetime, BaseModel, ValidationError

from debug_devices_mcp.multimeter import FrameReading, MeterMode, MeterResult, MultimeterReading
from debug_devices_mcp.sevenseg.constants import JSON_SUFFIX, LCD_SUFFIX, LEGACY_FRAME_SUFFIX, dataset
from debug_devices_mcp.sevenseg.decode import Gray, RegionScore
from debug_devices_mcp.sevenseg.reading import LocalReading

logger = logging.getLogger(__name__)


class VisionFields(BaseModel):
    """The part of a vision reading that the local decoder can compare."""

    readable: bool
    display_text: str
    unit: str
    # The LCD mode as the model read it (not a mode that the user confirmed).
    mode: MeterMode
    value: float | None = None
    confidence: float | None = None

    @classmethod
    def of_reading(cls, reading: MultimeterReading) -> VisionFields:
        return cls(
            readable=reading.readable,
            display_text=reading.display_text,
            unit=reading.unit,
            mode=reading.mode,
            value=reading.value,
            confidence=reading.confidence,
        )

    @classmethod
    def of_result(cls, result: MeterResult) -> VisionFields:
        """The combined result. With a user-confirmed mode, the model's own mode is in `model_mode`."""
        return cls(
            readable=result.readable,
            display_text=result.display_text,
            unit=result.unit,
            mode=result.model_mode or result.mode,
            value=result.value,
            confidence=result.confidence,
        )


class SavedImage(StrEnum):
    # The warped LCD image of the profile (`<entry_id>.lcd.png`): the LCD area only.
    LCD = "lcd"
    # No image: `note` says why (no webcam crop, or the frame does not match the profile).
    NONE = "none"


class DatasetEntry(BaseModel):
    entry_id: str
    saved_at: AwareDatetime
    capture_id: str | None
    captured_at: AwareDatetime | None
    # The calibration time of the profile that decoded the frame.
    profile_calibrated_at: AwareDatetime | None
    vision: VisionFields
    local: LocalReading
    regions: list[RegionScore]
    image: SavedImage = SavedImage.NONE
    note: str | None = None


def new_entry(
    vision: VisionFields,
    local: LocalReading,
    regions: list[RegionScore],
    frame: FrameReading | None,
    profile_calibrated_at: datetime | None,
) -> DatasetEntry:
    return DatasetEntry(
        entry_id=str(uuid.uuid7()),
        saved_at=datetime.now(UTC),
        capture_id=frame.capture_id if frame is not None else None,
        captured_at=frame.captured_at if frame is not None else None,
        profile_calibrated_at=profile_calibrated_at,
        vision=vision,
        local=local,
        regions=regions,
    )


UUID_V7 = 7


def entry_id_of(path: Path, suffix: str) -> str | None:
    """The entry id of a dataset file: a canonical UUID v7 before `suffix`. None for any other file."""
    if not path.name.endswith(suffix):
        return None
    stem = path.name.removesuffix(suffix)
    try:
        parsed = uuid.UUID(stem)
    except ValueError:
        return None
    return stem if parsed.version == UUID_V7 and str(parsed) == stem else None


def dataset_files(directory: Path, suffix: str) -> list[Path]:
    """The files of the dataset with this suffix (UUID v7 names only), oldest first."""
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.glob(f"*{suffix}") if entry_id_of(path, suffix) is not None)


class Pruned(BaseModel):
    """What a prune removed."""

    entries: int
    # Webcam frames (`<uuid>.jpg`) of an older version: the dataset keeps only LCD images now.
    legacy_frames: int
    orphan_images: int


class DatasetWriteError(OSError):
    """OpenCV could not encode or write the LCD image."""


class Dataset:
    def __init__(self, directory: Path, max_entries: int = dataset.MAX_ENTRIES) -> None:
        self.directory = directory
        self.max_entries = max_entries

    def image_path(self, entry_id: str) -> Path:
        return self.directory / f"{entry_id}{LCD_SUFFIX}"

    def add(self, entry: DatasetEntry, lcd: Gray | None, note: str | None = None) -> DatasetEntry:
        """Save the entry, with the warped LCD image when there is one (never a webcam frame).

        The JSON goes first: a failed image write leaves an entry that the prune removes with the others.
        """
        self.directory.mkdir(parents=True, exist_ok=True)
        entry = entry.model_copy(update={"image": SavedImage.NONE if lcd is None else SavedImage.LCD, "note": note})
        (self.directory / f"{entry.entry_id}{JSON_SUFFIX}").write_text(entry.model_dump_json() + "\n")
        if lcd is not None and not cv2.imwrite(str(self.image_path(entry.entry_id)), lcd):
            raise DatasetWriteError(f"cannot write the LCD image of {entry.entry_id} to {self.directory}")
        removed = self.prune()
        if removed.legacy_frames:
            logger.warning(
                "removed %d webcam frames of an older version from %s", removed.legacy_frames, self.directory
            )
        return entry

    def entry_paths(self) -> list[Path]:
        return dataset_files(self.directory, JSON_SUFFIX)

    def prune(self) -> Pruned:
        """Keep the newest `max_entries` entries. Remove older webcam frames and images without an entry.

        Only the compare writer calls it, for its own folder. It touches only files with a UUID v7 entry name.
        """
        paths = self.entry_paths()
        old_entries = paths[: max(0, len(paths) - self.max_entries)]
        for path in old_entries:
            path.unlink(missing_ok=True)
        entry_ids = {entry_id_of(path, JSON_SUFFIX) for path in self.entry_paths()}
        legacy = dataset_files(self.directory, LEGACY_FRAME_SUFFIX)
        for path in legacy:
            path.unlink(missing_ok=True)
        orphans = [
            path for path in dataset_files(self.directory, LCD_SUFFIX) if entry_id_of(path, LCD_SUFFIX) not in entry_ids
        ]
        for path in orphans:
            path.unlink(missing_ok=True)
        return Pruned(entries=len(old_entries), legacy_frames=len(legacy), orphan_images=len(orphans))

    def entries(self) -> Iterator[tuple[DatasetEntry, Path | None]]:
        """Each entry and the path of its LCD image (None without one), oldest first. A broken entry is skipped."""
        for path in self.entry_paths():
            try:
                entry = DatasetEntry.model_validate_json(path.read_bytes())
            except (OSError, ValidationError) as exc:
                logger.warning("skip the dataset entry %s: %s", path, exc)
                continue
            image = self.image_path(entry.entry_id)
            yield entry, image if entry.image is SavedImage.LCD and image.is_file() else None
