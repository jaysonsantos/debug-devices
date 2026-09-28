"""Webcam image controls (webcam_controls.py) and the dim-LCD hint of the meter check. Only a fake v4l2-ctl."""

from pathlib import Path

import pytest
from mcp import Client
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from debug_devices_mcp.multimeter import MeterMode, MultimeterReading, check_reading
from debug_devices_mcp.process import CommandResult
from debug_devices_mcp.webcam_controls import (
    V4l2Controls,
    WebcamControls,
    WebcamControlStore,
    parse_controls,
    register_webcam_control_tools,
)

from .conftest import FakeRunner
from .test_multimeter import READING

LIST_OUTPUT = b"""
User Controls

                     brightness 0x00980900 (int)    : min=-64 max=64 step=1 default=0 value=0 flags=has-min-max
                       contrast 0x00980901 (int)    : min=0 max=64 step=1 default=32 value=32 flags=has-min-max
                           gain 0x00980913 (int)    : min=0 max=100 step=1 default=0 value=0 flags=has-min-max

Camera Controls

                  auto_exposure 0x009a0901 (menu)   : min=0 max=3 default=3 value=3 (Aperture Priority Mode)
  exposure_time_absolute 0x009a0902 (int)    : min=1 max=5000 step=1 default=157 value=157 flags=inactive, has-min-max
"""
DEVICE = Path("/dev/video9")


def fake_v4l2() -> FakeRunner:
    def answer(command: list[str]) -> CommandResult:
        return CommandResult(returncode=0, stdout=LIST_OUTPUT if "--list-ctrls" in command else b"", stderr=b"")

    return FakeRunner(answer)


def set_calls(runner: FakeRunner) -> list[str]:
    return [command[command.index("--set-ctrl") + 1] for command in runner.calls if "--set-ctrl" in command]


def test_parse_controls() -> None:
    controls = parse_controls(LIST_OUTPUT.decode())
    assert controls["brightness"].minimum == -64
    assert controls["brightness"].maximum == 64
    assert controls["auto_exposure"].value == 3
    assert "inactive" in controls["exposure_time_absolute"].flags


def test_exposure_turns_the_automatic_exposure_off_first() -> None:
    assert WebcamControls(exposure=800).settings() == {"auto_exposure": 1, "exposure_time_absolute": 800}
    assert WebcamControls(auto_exposure=True).settings() == {"auto_exposure": 3}
    assert WebcamControls(brightness=20, contrast=40).settings() == {"brightness": 20, "contrast": 40}


async def test_update_sets_checks_and_persists(tmp_path: Path) -> None:
    runner = fake_v4l2()
    store = WebcamControlStore(tmp_path / "webcam-controls.json")
    controls = V4l2Controls(runner, DEVICE, store, "v4l2-ctl")

    report = await controls.update(WebcamControls(brightness=30, exposure=900))

    assert set_calls(runner) == ["brightness=30,auto_exposure=1,exposure_time_absolute=900"]
    assert all(command[:3] == ["v4l2-ctl", "-d", str(DEVICE)] for command in runner.calls)
    assert report.saved.brightness == 30
    assert WebcamControlStore(tmp_path / "webcam-controls.json").load().exposure == 900
    with pytest.raises(ToolError, match=r"brightness 100 is outside -64\.\.64"):
        await controls.update(WebcamControls(brightness=100))


async def test_saved_values_apply_once_per_process(tmp_path: Path) -> None:
    store = WebcamControlStore(tmp_path / "webcam-controls.json")
    store.save(WebcamControls(contrast=50))
    runner = fake_v4l2()
    controls = V4l2Controls(runner, DEVICE, store, "v4l2-ctl")

    await controls.ensure_applied()
    await controls.ensure_applied()

    assert set_calls(runner) == ["contrast=50"]


async def test_nothing_saved_runs_nothing() -> None:
    runner = fake_v4l2()
    await V4l2Controls(runner, DEVICE, WebcamControlStore(), "v4l2-ctl").ensure_applied()
    assert runner.calls == []


async def test_tool_reads_and_sets() -> None:
    runner = fake_v4l2()
    server = MCPServer("test")
    register_webcam_control_tools(server, V4l2Controls(runner, DEVICE, WebcamControlStore(), "v4l2-ctl"))

    async with Client(server) as client:
        read = await client.call_tool("webcam_controls", {})
        changed = await client.call_tool("webcam_controls", {"gain": 40})
        refused = await client.call_tool("webcam_controls", {"exposure": 99999})

    assert read.structured_content is not None
    assert [control["name"] for control in read.structured_content["controls"]] == [
        "brightness",
        "contrast",
        "gain",
        "auto_exposure",
        "exposure_time_absolute",
    ]
    assert changed.structured_content is not None
    assert changed.structured_content["saved"]["gain"] == 40
    assert set_calls(runner) == ["gain=40"]
    assert refused.is_error


def dim(meter_model: str) -> str | None:
    reading = MultimeterReading.model_validate(
        {**READING, "unit": "unknown", "confidence": 0.3, "notes": "The LCD is too dim to read the unit symbol."}
    )
    return check_reading(reading, MeterMode.RESISTANCE, meter_model).request


def test_dim_lcd_hint_names_the_backlight_and_the_exposure() -> None:
    request = dim("PROSTER T21D")
    assert request is not None
    assert "turn on the meter backlight (the PROSTER T21D backlight key)" in request
    assert "webcam_controls" in request
    generic = dim("")
    assert generic is not None
    assert "the backlight key of the meter" in generic


def test_no_dim_hint_for_a_clear_or_confirmed_reading() -> None:
    confirmed = check_reading(MultimeterReading.model_validate({**READING, "notes": "dim room, clear LCD"}))
    assert confirmed.request is None
    blurred = MultimeterReading.model_validate({**READING, "unit": "unknown", "confidence": 0.3, "notes": "blurred"})
    request = check_reading(blurred).request
    assert request is not None
    assert "backlight" not in request
