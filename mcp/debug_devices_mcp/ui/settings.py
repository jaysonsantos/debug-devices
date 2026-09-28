"""Settings that the user changes in the monitor window. They persist in a JSON file in the XDG state directory."""

import errno
import fcntl
import logging
import os
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import timedelta
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from debug_devices_mcp.images import SnapshotOrientation
from debug_devices_mcp.ui.constants import SETTINGS_FILE_NAME, STATE_DIR_NAME, defaults, env
from debug_devices_mcp.webcam import Crop

logger = logging.getLogger(__name__)

TEMP_SUFFIX = ".tmp"
# The lock file next to the settings file: every read-change-save of any MCP server holds it (B-W9 of QA round 4).
LOCK_SUFFIX = ".lock"
# A writer holds the lock only for one read and one write. After this time, the save fails (an OSError).
LOCK_TIMEOUT = timedelta(seconds=2)
LOCK_RETRY = timedelta(milliseconds=10)


class ScreenRotation(StrEnum):
    """How the page turns the phone screen: with the phone (auto), or a fixed clockwise angle."""

    AUTO = "auto"
    DEG_0 = "0"
    DEG_90 = "90"
    DEG_180 = "180"
    DEG_270 = "270"


class UiSettings(BaseModel):
    """Values saved from the page. None means: use the value from the CLI flag, the variable, or the default."""

    model_config = ConfigDict(extra="ignore")

    vision_model: str | None = Field(default=None, min_length=1)
    webcam_warmup_frames: int | None = Field(default=None, ge=0)
    webcam_crop: Crop | None = None
    screen_rotation: ScreenRotation | None = None
    # The flips of the phone snapshot. `OrientationState` owns this value; the other settings keep it as it is.
    snapshot_orientation: SnapshotOrientation | None = None
    # The in-sensor zoom choice. `InSensorZoomChoice` owns it, like the flips.
    in_sensor_zoom: bool | None = None
    # The phone that the user selected in the page (adb serial). It replaces DEBUG_DEVICES_ADB_SERIAL while set.
    adb_serial: str | None = Field(default=None, min_length=1)
    # Show the markings (boxes, arrows, labels, frames) on the page and the phone. `MarkingsChoice` owns it.
    markings_visible: bool | None = None
    # The autofocus mode choice. `AfModeChoice` owns it.
    af_mode: Literal["continuous", "macro"] | None = None


class EffectiveSettings(BaseModel):
    """The values in use: the saved ones over the start values."""

    vision_model: str
    webcam_warmup_frames: int
    webcam_crop: Crop | None
    screen_rotation: ScreenRotation = ScreenRotation.AUTO

    def with_saved(self, saved: UiSettings) -> EffectiveSettings:
        return EffectiveSettings(
            vision_model=saved.vision_model or self.vision_model,
            webcam_warmup_frames=(
                self.webcam_warmup_frames if saved.webcam_warmup_frames is None else saved.webcam_warmup_frames
            ),
            webcam_crop=saved.webcam_crop or self.webcam_crop,
            screen_rotation=saved.screen_rotation or self.screen_rotation,
        )


def state_dir(environ: Mapping[str, str] = os.environ) -> Path:
    """`$XDG_STATE_HOME/debug-devices`, or `~/.local/state/debug-devices`."""
    base = environ.get(env.XDG_STATE_HOME) or str(Path(environ.get(env.HOME) or Path.home()) / defaults.STATE_HOME)
    return Path(base) / STATE_DIR_NAME


class SettingsStore:
    """The settings file. It is the one source of truth for every MCP server of this user: `current()` reads it
    again when it changed, so a change through another server is seen here too."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._cached: UiSettings | None = None
        self._stamp: tuple[int, int, int] | None = None

    def _file_stamp(self) -> tuple[int, int, int] | None:
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size, stat.st_ino)

    def current(self) -> UiSettings:
        """The saved settings, read again only when the file changed (time, size, or a new file)."""
        stamp = self._file_stamp()
        if self._cached is None or stamp != self._stamp:
            self._cached = self.load()
            self._stamp = stamp
        return self._cached

    @classmethod
    def in_dir(cls, directory: Path) -> SettingsStore:
        return cls(directory / SETTINGS_FILE_NAME)

    def load(self) -> UiSettings:
        """A missing or broken file gives the empty settings. A broken file is logged, not fatal."""
        try:
            return UiSettings.model_validate_json(self.path.read_bytes())
        except FileNotFoundError:
            return UiSettings()
        except (OSError, ValidationError) as exc:
            logger.warning("ignoring the monitor settings in %s: %s", self.path, exc)
            return UiSettings()

    def update(self, change: Callable[[UiSettings], UiSettings]) -> UiSettings:
        """Read the file, change it, and save it under the lock file: two MCP servers (or a server and its page)
        never lose each other's change. Raises OSError when the file cannot be locked or written."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._locked():
            updated = change(self.load())
            self.save(updated)
        return updated

    @contextmanager
    def _locked(self) -> Iterator[None]:
        lock_path = self.path.with_name(f"{self.path.name}{LOCK_SUFFIX}")
        with lock_path.open("a") as handle:
            deadline = time.monotonic() + LOCK_TIMEOUT.total_seconds()
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise OSError(errno.EWOULDBLOCK, f"the settings file is locked: {lock_path}") from None
                    time.sleep(LOCK_RETRY.total_seconds())
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def save(self, settings: UiSettings) -> None:
        """Write a temporary file, then rename it, so a crash never leaves half a file. A change of one value goes
        through `update` (it holds the lock)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # One temporary name per process: two MCP servers can save at the same time.
        temp = self.path.with_name(f"{self.path.name}.{os.getpid()}{TEMP_SUFFIX}")
        temp.write_text(settings.model_dump_json(indent=2, exclude_none=True))
        temp.replace(self.path)
        self._cached, self._stamp = settings, self._file_stamp()
