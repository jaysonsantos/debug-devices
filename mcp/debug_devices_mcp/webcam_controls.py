"""Webcam image controls for the meter (brightness, contrast, gain, exposure) through `v4l2-ctl`.

A dim LCD is hard to read. These controls change the webcam image at the device level (V4L2), so they also apply
while the shared webcam stream runs. The values persist in `webcam-controls.json` in the user's state directory and
go to the webcam again one time per server process, before the first webcam use (the driver can lose them).
"""

import contextlib
import json
import logging
import re
from datetime import timedelta
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ValidationError

from debug_devices_mcp.process import CommandError, CommandRunner

logger = logging.getLogger(__name__)

CONTROLS_FILE_NAME = "webcam-controls.json"
DEFAULT_V4L2_CTL = "v4l2-ctl"
DEVICE_FLAG = "-d"
LIST_FLAG = "--list-ctrls"
SET_FLAG = "--set-ctrl"
TIMEOUT = timedelta(seconds=5)

# V4L2 control names.
BRIGHTNESS = "brightness"
CONTRAST = "contrast"
GAIN = "gain"
AUTO_EXPOSURE = "auto_exposure"
EXPOSURE = "exposure_time_absolute"
# auto_exposure menu values (UVC): 1 manual, 3 aperture priority (automatic).
AUTO_EXPOSURE_MANUAL = 1
AUTO_EXPOSURE_AUTOMATIC = 3

# `brightness 0x00980900 (int)    : min=-64 max=64 step=1 default=0 value=0 flags=has-min-max`
CONTROL_LINE = re.compile(r"^\s*(?P<name>\w+)\s+0x[0-9a-f]+\s+\((?P<kind>\w+)\)\s*:\s*(?P<fields>.*)$")
FIELD = re.compile(r"(\w+)=(-?\d+)")


class ControlInfo(BaseModel):
    name: str
    kind: str
    value: int | None = None
    minimum: int | None = None
    maximum: int | None = None
    default: int | None = None
    # For example "inactive": the exposure time is inactive while the automatic exposure is on.
    flags: str = ""


class WebcamControls(BaseModel):
    """The values that the user or the agent chose. None: leave that control as it is."""

    brightness: int | None = None
    contrast: int | None = None
    gain: int | None = None
    # True: the camera sets the exposure. False: `exposure` (in 100 µs units, UVC) is fixed.
    auto_exposure: bool | None = None
    exposure: int | None = None

    def settings(self) -> dict[str, int]:
        """The V4L2 control values to set, in a safe order (the exposure mode before the exposure time)."""
        values: dict[str, int] = {}
        for name, value in ((BRIGHTNESS, self.brightness), (CONTRAST, self.contrast), (GAIN, self.gain)):
            if value is not None:
                values[name] = value
        if self.auto_exposure is not None:
            values[AUTO_EXPOSURE] = AUTO_EXPOSURE_AUTOMATIC if self.auto_exposure else AUTO_EXPOSURE_MANUAL
        if self.exposure is not None:
            values.setdefault(AUTO_EXPOSURE, AUTO_EXPOSURE_MANUAL)
            values[EXPOSURE] = self.exposure
        return values


class ControlsReport(BaseModel):
    device: str
    saved: WebcamControls
    controls: list[ControlInfo]


def parse_controls(output: str) -> dict[str, ControlInfo]:
    controls = {}
    for line in output.splitlines():
        match = CONTROL_LINE.match(line)
        if match is None:
            continue
        fields = dict(FIELD.findall(match["fields"]))
        flags = match["fields"].split("flags=", 1)[1].strip() if "flags=" in match["fields"] else ""
        controls[match["name"]] = ControlInfo(
            name=match["name"],
            kind=match["kind"],
            value=int(fields["value"]) if "value" in fields else None,
            minimum=int(fields["min"]) if "min" in fields else None,
            maximum=int(fields["max"]) if "max" in fields else None,
            default=int(fields["default"]) if "default" in fields else None,
            flags=flags,
        )
    return controls


class WebcamControlStore:
    """The saved values (a small JSON file). Without a path: memory only (tests)."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._memory = WebcamControls()

    def load(self) -> WebcamControls:
        if self.path is None:
            return self._memory
        try:
            return WebcamControls.model_validate_json(self.path.read_bytes())
        except FileNotFoundError:
            return WebcamControls()
        except (OSError, ValidationError, json.JSONDecodeError) as exc:
            logger.warning("cannot read %s, using no webcam controls: %s", self.path, exc)
            return WebcamControls()

    def save(self, controls: WebcamControls) -> None:
        if self.path is None:
            self._memory = controls
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(controls.model_dump_json(indent=2) + "\n")
        temporary.replace(self.path)


class V4l2Controls:
    def __init__(self, runner: CommandRunner, device: Path, store: WebcamControlStore, v4l2_ctl: str) -> None:
        self._runner = runner
        self.device = device
        self.store = store
        self._v4l2_ctl = v4l2_ctl
        self._applied = False

    async def _run(self, *args: str) -> str:
        command = [self._v4l2_ctl, DEVICE_FLAG, str(self.device), *args]
        try:
            result = await self._runner.run(command, TIMEOUT)
        except CommandError as exc:
            raise ToolError(f"v4l2-ctl: {exc}") from exc
        if not result.ok:
            raise ToolError(
                f"v4l2-ctl exited with {result.returncode}: {result.stderr.decode(errors='replace').strip()}"
            )
        return result.stdout.decode(errors="replace")

    async def read(self) -> dict[str, ControlInfo]:
        return parse_controls(await self._run(LIST_FLAG))

    async def set(self, values: dict[str, int], known: dict[str, ControlInfo]) -> None:
        problems = []
        for name, value in values.items():
            info = known.get(name)
            if info is None:
                problems.append(f"this webcam has no {name} control")
            elif info.minimum is not None and info.maximum is not None and not info.minimum <= value <= info.maximum:
                problems.append(f"{name} {value} is outside {info.minimum}..{info.maximum}")
        if problems:
            raise ToolError("; ".join(problems))
        if values:
            await self._run(SET_FLAG, ",".join(f"{name}={value}" for name, value in values.items()))

    async def ensure_applied(self) -> None:
        """Send the saved values one time per process, before the first webcam use. A failure only logs."""
        if self._applied:
            return
        self._applied = True
        values = self.store.load().settings()
        if not values:
            return
        with contextlib.suppress(ToolError):
            await self.set(values, await self.read())
            return
        logger.warning("cannot apply the saved webcam controls to %s", self.device)

    async def update(self, changes: WebcamControls) -> ControlsReport:
        saved = self.store.load()
        merged = saved.model_copy(update=changes.model_dump(exclude_none=True))
        known = await self.read()
        await self.set(changes.settings(), known)
        self.store.save(merged)
        self._applied = True
        return await self.report()

    async def report(self) -> ControlsReport:
        known = await self.read()
        names = (BRIGHTNESS, CONTRAST, GAIN, AUTO_EXPOSURE, EXPOSURE)
        return ControlsReport(
            device=str(self.device), saved=self.store.load(), controls=[known[name] for name in names if name in known]
        )


def register_webcam_control_tools(server: MCPServer, controls: V4l2Controls) -> None:
    @server.tool()
    async def webcam_controls(
        brightness: int | None = None,
        contrast: int | None = None,
        gain: int | None = None,
        auto_exposure: bool | None = None,
        exposure: int | None = None,
    ) -> ControlsReport:
        """Read or set the image controls of the meter webcam (V4L2): brightness, contrast, gain, and exposure.

        No argument: only read (the current values and their ranges). Use it when multimeter_read says that the LCD is
        dim: first ask the user to turn on the meter backlight, then raise the exposure (`exposure` sets a fixed
        exposure time and turns the automatic exposure off; `auto_exposure: true` turns it on again) or the
        brightness. The values persist and apply again after a restart. Then check the framing with webcam_snapshot
        and read the meter again with multimeter_read.
        """
        changes = WebcamControls(
            brightness=brightness, contrast=contrast, gain=gain, auto_exposure=auto_exposure, exposure=exposure
        )
        if not changes.settings():
            return await controls.report()
        return await controls.update(changes)
