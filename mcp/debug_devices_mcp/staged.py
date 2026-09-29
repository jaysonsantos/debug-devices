"""Staged captures (docs/briefs/staged-captures.md): the phone photo and the multimeter reading of one key press on
the monitor page, kept until the agent's next `multimeter_read`.

The agent cannot be woken by the MCP server, so the captures wait in a queue. The store is a folder in the state
folder (`~/.local/state/debug-devices/staged/`, files with mode 0600) with a file lock, so every MCP server of the
user (the page server, a secondary, the bench session) can list and pop them. One capture is a JSON entry, the
photo, and the meter frames that the vision model saw (only the crop box). At most MAX_STAGED captures, each for
STAGED_TTL.

The bench gate does not wait for the queue: the capture runs the same meter path as `multimeter_read`, so an unsafe
voltage closes the gate at capture time.
"""

import asyncio
import contextlib
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ValidationError

from debug_devices_mcp.file_lock import locked
from debug_devices_mcp.multimeter import MeterResult
from debug_devices_mcp.ui.settings import state_dir

# region: constants

STAGED_DIR_NAME = "staged"
LOCK_FILE_NAME = ".lock"
ENTRY_SUFFIX = ".json"
PHOTO_SUFFIX = ".photo.jpg"
FRAME_SUFFIX = ".meter-{index}.jpg"
TEMP_SUFFIX = ".tmp"
FILE_MODE = 0o600
DIR_MODE = 0o700
# A full queue refuses a new capture (the page says so).
MAX_STAGED = 10
# A capture older than this is gone (nobody asked for it).
STAGED_TTL = timedelta(minutes=30)
# A capture that is still pending after this time did not finish (its server stopped): it becomes failed.
PENDING_LIMIT = timedelta(minutes=3)
# While a capture is pending (the vision call runs), a pop waits for it this long at most, checking this often.
PENDING_WAIT = timedelta(seconds=90)
PENDING_POLL = timedelta(milliseconds=250)
QUEUE_FULL = "{count} staged captures wait already (at most {limit}): ask the agent to read them (multimeter_read)"
PENDING_TIMED_OUT = "the capture did not finish (its MCP server stopped, or the vision call hung)"
STAGED_LIST_NOTE = (
    "Staged captures that wait, oldest first (they stay). multimeter_read returns them with their images and removes "
    "them. Each shows the moment of its capture, not now."
)
STAGED_NOTE = (
    "Staged captures from the monitor page, oldest first. Each photo and meter value shows the moment of its "
    "capture (`captured_at`, `age_s`), not now: say so. They are removed from the queue now. To record one "
    "(bench_record_measurement), pass its `capture_id`."
)

# endregion: constants


class StagedState(StrEnum):
    # The key press is taken; the photo and the vision call still run.
    PENDING = "pending"
    READY = "ready"
    # The capture ended without a meter result or a photo; `notes` say why.
    FAILED = "failed"


class StagedPhoto(BaseModel):
    """The phone photo of the capture, as phone_snapshot shows it (the turn and the flips of that moment)."""

    width: int
    height: int
    turn_degrees: int
    flip_horizontal: bool
    flip_vertical: bool


class StagedCapture(BaseModel):
    capture_id: str
    captured_at: AwareDatetime
    state: StagedState = StagedState.PENDING
    # The MCP server that took it (its origin label).
    origin: str = ""
    photo: StagedPhoto | None = None
    # The same result as multimeter_read (with its bench notice), or None: no crop box, no webcam, an error.
    meter: MeterResult | None = None
    # The number of meter frames (the images that the vision model saw) that the store keeps.
    meter_frames: int = 0
    notes: list[str] = []

    def age_seconds(self, now: datetime) -> float:
        return round((now - self.captured_at).total_seconds(), 1)

    def meter_image_index(self) -> int | None:
        """The kept frame that gave the meter result (its `capture_id`: meter_frames.combine takes the first frame),
        or None without a meter result or frames. The frames are crops only: never the full webcam frame."""
        if self.meter is None or self.meter_frames == 0:
            return None
        frame_ids = [frame.capture_id for frame in self.meter.frames]
        index = frame_ids.index(self.meter.capture_id) if self.meter.capture_id in frame_ids else 0
        return index if index < self.meter_frames else None


@dataclass
class StagedItem:
    """A popped capture with its images."""

    capture: StagedCapture
    photo: bytes | None = None
    # All kept meter frames, in capture order (None for a frame file that is gone).
    frames: list[bytes | None] = field(default_factory=list)

    @property
    def meter_image(self) -> bytes | None:
        """The meter crop image of the frame that gave the reading."""
        index = self.capture.meter_image_index()
        return self.frames[index] if index is not None and index < len(self.frames) else None

    @property
    def other_frames(self) -> list[bytes]:
        """The kept meter frames without the meter crop image (include_image adds them)."""
        used = self.capture.meter_image_index()
        return [frame for index, frame in enumerate(self.frames) if index != used and frame is not None]


class StagedReading(BaseModel):
    """One staged capture in a tool result (its images follow as image blocks with its `capture_id`)."""

    capture_id: str
    captured_at: AwareDatetime
    # Seconds from the capture to this result: the photo and the value show that moment, not now.
    age_s: float
    state: StagedState
    origin: str
    photo: StagedPhoto | None
    meter: MeterResult | None
    notes: list[str]

    @classmethod
    def of(cls, capture: StagedCapture, now: datetime) -> StagedReading:
        return cls(
            capture_id=capture.capture_id,
            captured_at=capture.captured_at,
            age_s=capture.age_seconds(now),
            state=capture.state,
            origin=capture.origin,
            photo=capture.photo,
            meter=capture.meter,
            notes=capture.notes,
        )


class StagedReadResult(BaseModel):
    """multimeter_read with staged captures: all of them, oldest first (they are removed from the queue)."""

    staged: list[StagedReading]
    count: int
    note: str = STAGED_NOTE
    # The multimeter_read parameters that were given but apply only to a live read (N93).
    not_applied: list[str] = []
    not_applied_note: str | None = None


class StagedCapturesResult(BaseModel):
    """staged_captures: the waiting captures, oldest first (read only: they stay in the queue)."""

    captures: list[StagedReading]
    count: int
    note: str = STAGED_LIST_NOTE


class QueueFullError(Exception):
    """MAX_STAGED captures wait already."""


class Capturer(Protocol):
    """What the page routes need from the capture (ui/staged_capture.py): the store, and a new capture."""

    store: StagedStore

    async def capture(self) -> StagedCapture: ...


type Clock = Callable[[], datetime]


def is_capture_id(value: str) -> bool:
    """A capture id is a UUID in its standard form: nothing else becomes part of a file name."""
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def timed_out(capture: StagedCapture) -> StagedCapture:
    """A pending capture that did not finish in time: failed, with the reason."""
    return capture.model_copy(update={"state": StagedState.FAILED, "notes": [*capture.notes, PENDING_TIMED_OUT]})


def utc_now() -> datetime:
    return datetime.now(UTC)


class StagedStore:
    """The queue folder. The methods without a leading underscore are for the event loop: the lock wait and the file
    work run in a worker thread."""

    def __init__(self, directory: Path, clock: Clock = utc_now) -> None:
        self.directory = directory
        self._clock = clock
        # Called after a change by this process, so the page gets the new list at once. The changes of other MCP
        # server processes show in `version`.
        self.listeners: list[Callable[[], None]] = []

    @classmethod
    def default(cls) -> StagedStore:
        """The folder in the state folder: shared by every MCP server of the user."""
        return cls(state_dir() / STAGED_DIR_NAME)

    # region: async API

    async def add(self, capture: StagedCapture) -> None:
        """Add a new (pending) capture. Raises QueueFullError when MAX_STAGED wait already."""
        await asyncio.to_thread(self._add, capture)
        self._tell()

    async def finish(self, capture: StagedCapture, photo: bytes | None, frames: list[bytes]) -> bool:
        """Store the result of a capture. False: it is gone (deleted, popped, or expired meanwhile)."""
        return self._changed(await asyncio.to_thread(self._finish, capture, photo, frames))

    async def list(self) -> list[StagedCapture]:
        """The waiting captures, oldest first (also the pending ones)."""
        return await asyncio.to_thread(self._list)

    async def pop_ready(self) -> list[StagedItem]:
        """Remove and return the finished captures (ready or failed), oldest first. Pending ones stay."""
        return self._changed(await asyncio.to_thread(self._pop_ready))

    async def pop_all(self, wait: timedelta = PENDING_WAIT) -> list[StagedItem]:
        """Wait (at most `wait`) until no capture is pending, then remove and return all, oldest first. A capture
        that is still pending then comes as failed."""
        deadline = asyncio.get_running_loop().time() + wait.total_seconds()
        while any(item.state is StagedState.PENDING for item in await self.list()):
            if asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(PENDING_POLL.total_seconds())
        return self._changed(await asyncio.to_thread(self._pop_all))

    async def delete(self, capture_id: str) -> bool:
        if not is_capture_id(capture_id):
            return False
        return self._changed(await asyncio.to_thread(self._delete, capture_id))

    async def clear(self) -> int:
        return self._changed(await asyncio.to_thread(self._clear))

    async def version(self) -> tuple[int, frozenset[str]] | None:
        """Changes when any MCP server process adds, finishes, pops, or deletes a capture: the folder time and its
        file names (a change inside one clock tick of the file system still changes the names). None: no folder."""
        return await asyncio.to_thread(self._version)

    async def photo(self, capture_id: str) -> bytes | None:
        """The photo of a capture. Only a capture id (a UUID) names a file: never a path (N90)."""
        if not is_capture_id(capture_id):
            return None
        return await asyncio.to_thread(self._read, self._photo_path(capture_id))

    async def meter_image(self, capture_id: str) -> bytes | None:
        """The meter crop image of a capture (the frame of its reading). Only a capture id names a file (N90)."""
        if not is_capture_id(capture_id):
            return None
        return await asyncio.to_thread(self._meter_image, capture_id)

    def _changed[T](self, result: T) -> T:
        """Tell the listeners when the change did something (a result that is not empty, zero, or False)."""
        if result:
            self._tell()
        return result

    def _tell(self) -> None:
        for listener in self.listeners:
            listener()

    # endregion: async API

    # region: files (call with the lock)

    def _entry_path(self, capture_id: str) -> Path:
        return self.directory / f"{capture_id}{ENTRY_SUFFIX}"

    def _photo_path(self, capture_id: str) -> Path:
        return self.directory / f"{capture_id}{PHOTO_SUFFIX}"

    def _frame_path(self, capture_id: str, index: int) -> Path:
        return self.directory / f"{capture_id}{FRAME_SUFFIX.format(index=index)}"

    def _locked(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        os.chmod(self.directory, DIR_MODE)
        return locked(self.directory / LOCK_FILE_NAME, "the staged captures")

    def _write(self, path: Path, data: bytes) -> None:
        """A private file (mode 0600), written to a temporary name first, so a reader never sees half a file."""
        temp = path.with_name(f"{path.name}.{os.getpid()}{TEMP_SUFFIX}")
        with contextlib.suppress(FileNotFoundError):
            temp.unlink()
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
        with os.fdopen(fd, "wb") as file:
            file.write(data)
        temp.replace(path)

    @staticmethod
    def _read(path: Path) -> bytes | None:
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None

    def _version(self) -> tuple[int, frozenset[str]] | None:
        try:
            return self.directory.stat().st_mtime_ns, frozenset(os.listdir(self.directory))
        except FileNotFoundError:
            return None

    def _entries(self) -> list[StagedCapture]:
        """All entries, oldest first. A broken entry is removed."""
        entries = []
        for path in self.directory.glob(f"*{ENTRY_SUFFIX}"):
            try:
                entries.append(StagedCapture.model_validate_json(path.read_bytes()))
            except OSError, ValidationError:
                path.unlink(missing_ok=True)
        return sorted(entries, key=lambda entry: (entry.captured_at, entry.capture_id))

    def _remove_files(self, capture: StagedCapture) -> None:
        self._entry_path(capture.capture_id).unlink(missing_ok=True)
        self._photo_path(capture.capture_id).unlink(missing_ok=True)
        for index in range(capture.meter_frames):
            self._frame_path(capture.capture_id, index).unlink(missing_ok=True)

    def _sweep(self) -> list[StagedCapture]:
        """Remove the expired captures, fail the pending ones that never finished, and return the rest."""
        now = self._clock()
        kept = []
        for entry in self._entries():
            age = now - entry.captured_at
            if age > STAGED_TTL:
                self._remove_files(entry)
                continue
            if entry.state is StagedState.PENDING and age > PENDING_LIMIT:
                failed = timed_out(entry)
                self._write(self._entry_path(failed.capture_id), failed.model_dump_json().encode())
                kept.append(failed)
                continue
            kept.append(entry)
        return kept

    def _item(self, capture: StagedCapture) -> StagedItem:
        return StagedItem(
            capture=capture,
            photo=self._read(self._photo_path(capture.capture_id)),
            frames=[self._read(self._frame_path(capture.capture_id, index)) for index in range(capture.meter_frames)],
        )

    def _meter_image(self, capture_id: str) -> bytes | None:
        """Without the lock: each file is replaced whole (`_write`), so a reader never sees half a file."""
        entry = self._read(self._entry_path(capture_id))
        if entry is None:
            return None
        try:
            index = StagedCapture.model_validate_json(entry).meter_image_index()
        except ValidationError:
            return None
        return None if index is None else self._read(self._frame_path(capture_id, index))

    # endregion: files

    # region: sync work (worker thread)

    def _add(self, capture: StagedCapture) -> None:
        with self._locked():
            waiting = self._sweep()
            if len(waiting) >= MAX_STAGED:
                raise QueueFullError(QUEUE_FULL.format(count=len(waiting), limit=MAX_STAGED))
            self._write(self._entry_path(capture.capture_id), capture.model_dump_json().encode())

    def _finish(self, capture: StagedCapture, photo: bytes | None, frames: list[bytes]) -> bool:
        with self._locked():
            if not self._entry_path(capture.capture_id).exists():
                return False
            if photo is not None:
                self._write(self._photo_path(capture.capture_id), photo)
            for index, frame in enumerate(frames):
                self._write(self._frame_path(capture.capture_id, index), frame)
            finished = capture.model_copy(update={"meter_frames": len(frames)})
            self._write(self._entry_path(capture.capture_id), finished.model_dump_json().encode())
            return True

    def _list(self) -> list[StagedCapture]:
        with self._locked():
            return self._sweep()

    def _pop_ready(self) -> list[StagedItem]:
        with self._locked():
            ready = [entry for entry in self._sweep() if entry.state is not StagedState.PENDING]
            items = [self._item(entry) for entry in ready]
            for entry in ready:
                self._remove_files(entry)
            return items

    def _pop_all(self) -> list[StagedItem]:
        with self._locked():
            items = []
            for entry in self._sweep():
                items.append(self._item(timed_out(entry) if entry.state is StagedState.PENDING else entry))
                self._remove_files(entry)
            return items

    def _delete(self, capture_id: str) -> bool:
        with self._locked():
            for entry in self._entries():
                if entry.capture_id == capture_id:
                    self._remove_files(entry)
                    return True
            return False

    def _clear(self) -> int:
        with self._locked():
            entries = self._entries()
            for entry in entries:
                self._remove_files(entry)
            return len(entries)

    # endregion: sync work
