"""The compare-mode dataset: each meter frame with the vision reading and the local reading (local, git-ignored).

It lives in the state folder, never in the repo. The frames are the webcam crop only (the area that already goes to
the vision model). At most `max_entries` frames stay: the oldest go first (the ids are UUID v7, so the name order is
the time order).
"""

import logging
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from pydantic import AwareDatetime, BaseModel, ValidationError

from debug_devices_mcp.multimeter import FrameReading, MeterMode, MeterResult, MultimeterReading
from debug_devices_mcp.sevenseg.constants import JPEG_SUFFIX, JSON_SUFFIX, dataset
from debug_devices_mcp.sevenseg.decode import RegionScore
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


class Dataset:
    def __init__(self, directory: Path, max_entries: int = dataset.MAX_ENTRIES) -> None:
        self.directory = directory
        self.max_entries = max_entries

    def add(self, entry: DatasetEntry, jpeg: bytes) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / f"{entry.entry_id}{JPEG_SUFFIX}").write_bytes(jpeg)
        (self.directory / f"{entry.entry_id}{JSON_SUFFIX}").write_text(entry.model_dump_json() + "\n")
        self.prune()

    def entry_paths(self) -> list[Path]:
        if not self.directory.is_dir():
            return []
        return sorted(self.directory.glob(f"*{JSON_SUFFIX}"))

    def prune(self) -> None:
        paths = self.entry_paths()
        for path in paths[: max(0, len(paths) - self.max_entries)]:
            path.unlink(missing_ok=True)
            path.with_suffix(JPEG_SUFFIX).unlink(missing_ok=True)

    def entries(self) -> Iterator[tuple[DatasetEntry, Path]]:
        """Each entry and the path of its frame, oldest first. A broken entry is skipped with a warning."""
        for path in self.entry_paths():
            try:
                entry = DatasetEntry.model_validate_json(path.read_bytes())
            except (OSError, ValidationError) as exc:
                logger.warning("skip the dataset entry %s: %s", path, exc)
                continue
            yield entry, path.with_suffix(JPEG_SUFFIX)
