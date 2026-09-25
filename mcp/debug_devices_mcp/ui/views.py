"""JSON shapes of the monitor web API."""

from pydantic import BaseModel, StrictBool

from debug_devices_mcp.phone_api import RotationDegrees, ZoomStep
from debug_devices_mcp.ui.events import PhoneState, ToolCallEvent
from debug_devices_mcp.ui.settings import EffectiveSettings, UiSettings
from debug_devices_mcp.webcam_stream import StreamInfo


class SettingsView(BaseModel):
    saved: UiSettings
    effective: EffectiveSettings
    start: EffectiveSettings
    settings_file: str


class StateView(BaseModel):
    phone: PhoneState
    settings: SettingsView
    webcam: StreamInfo | None
    calls: list[ToolCallEvent]


class ErrorView(BaseModel):
    error: str


class ZoomBody(BaseModel):
    ratio: float | None = None
    step: ZoomStep | None = None


class TorchBody(BaseModel):
    enabled: bool


class OrientationBody(BaseModel):
    """The snapshot flips. A missing field keeps that flip."""

    flip_horizontal: bool | None = None
    flip_vertical: bool | None = None


class InSensorZoomBody(BaseModel):
    # Strict: "yes" or 1 must not turn a camera mode on.
    enabled: StrictBool


class RotationBody(BaseModel):
    """The snapshot rotation lock: `degrees` locks it, `auto: true` follows the phone again."""

    degrees: RotationDegrees | None = None
    auto: bool = False
