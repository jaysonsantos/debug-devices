"""Settings that the user changes in the monitor window. They persist in a JSON file in the XDG state directory."""

import logging
import os
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from debug_devices_mcp.images import SnapshotOrientation
from debug_devices_mcp.ui.constants import SETTINGS_FILE_NAME, STATE_DIR_NAME, defaults, env
from debug_devices_mcp.webcam import Crop

logger = logging.getLogger(__name__)

TEMP_SUFFIX = ".tmp"


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
    def __init__(self, path: Path) -> None:
        self.path = path

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

    def save(self, settings: UiSettings) -> None:
        """Write a temporary file, then rename it, so a crash never leaves half a file."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + TEMP_SUFFIX)
        temp.write_text(settings.model_dump_json(indent=2, exclude_none=True))
        temp.replace(self.path)
