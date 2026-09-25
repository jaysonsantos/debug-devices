"""Phone distance and detail from the camera status: how far the phone is from the board, and how much detail it sees.

Pure math on `CameraStatus.focus` and `CameraStatus.optics` (docs/phone-api.md). The detail is a thin-lens estimate:
`output_width_px * focal_length_mm / (sensor_width_mm * distance_mm)`. Zoom does not change it: zoom crops.
"""

from enum import StrEnum

from pydantic import BaseModel

from debug_devices_mcp.phone_api import CameraStatus, FocusCalibration, InSensorZoom, Optics

CM_PER_METER = 100
MM_PER_CM = 10
# Enough detail to read small component markings in a snapshot.
GOOD_DETAIL_PX_PER_MM = 20.0
# The advice "move closer" aims a little farther than the closest focus distance, so the lens can still focus.
CLOSER_FACTOR = 1.1
# Distances below this show one decimal (for example 9.5 cm); above it, whole cm.
DECIMAL_BELOW_CM = 10
DISTANCE_DECIMALS_NEAR = 1
DETAIL_DECIMALS = 1
# With the in-sensor zoom on, a zoom from this ratio gives real extra detail (the estimate does not include it).
SENSOR_ZOOM_MIN_RATIO = 2.0


class Advice(StrEnum):
    TOO_CLOSE = "too_close"
    GOOD = "good"
    FAR = "far"
    UNKNOWN = "unknown"


class FocusReport(BaseModel):
    """Derived values for the agent (phone_status, bench_start) and for the monitor page."""

    distance_cm: float | None
    detail_px_per_mm: float | None
    min_distance_cm: float | None
    focus_state: str | None
    calibration: str | None
    advice: Advice
    advice_text: str
    # True when the in-sensor zoom is on and the zoom is 2x or more: the real detail is higher than the estimate.
    sensor_zoom_boost: bool = False


def distance_cm(diopters: float | None) -> float | None:
    """100 / diopters. None for no value, and for 0 (infinity)."""
    if diopters is None or diopters <= 0:
        return None
    return CM_PER_METER / diopters


def detail_px_per_mm(optics: Optics | None, distance: float | None) -> float | None:
    if optics is None or distance is None or distance <= 0 or optics.sensor_width_mm <= 0:
        return None
    return optics.output_width_px * optics.focal_length_mm / (optics.sensor_width_mm * distance * MM_PER_CM)


def round_distance(value: float) -> float:
    return round(value, DISTANCE_DECIMALS_NEAR) if value < DECIMAL_BELOW_CM else float(round(value))


def round_detail(value: float) -> float:
    return round(value, DETAIL_DECIMALS)


def focus_report(status: CameraStatus) -> FocusReport:
    boost = status.in_sensor_zoom is InSensorZoom.ON and status.zoom_ratio >= SENSOR_ZOOM_MIN_RATIO
    return distance_report(status).model_copy(update={"sensor_zoom_boost": boost})


def distance_report(status: CameraStatus) -> FocusReport:
    focus = status.focus
    if focus is None:
        return FocusReport(
            distance_cm=None,
            detail_px_per_mm=None,
            min_distance_cm=None,
            focus_state=None,
            calibration=None,
            advice=Advice.UNKNOWN,
            advice_text="the phone app sends no focus data",
        )
    base = {"focus_state": focus.state.value, "calibration": focus.calibration.value}
    if focus.calibration is FocusCalibration.UNCALIBRATED:
        # The lens distance is not in real units: no cm, no detail, no advice.
        return FocusReport(
            distance_cm=None,
            detail_px_per_mm=None,
            min_distance_cm=None,
            advice=Advice.UNKNOWN,
            advice_text="the lens distance is not calibrated on this phone",
            **base,
        )
    distance = distance_cm(focus.distance_diopters)
    detail = detail_px_per_mm(status.optics, distance)
    closest = distance_cm(focus.min_distance_diopters)
    report = {
        "distance_cm": round_distance(distance) if distance is not None else None,
        "detail_px_per_mm": round_detail(detail) if detail is not None else None,
        "min_distance_cm": round_distance(closest) if closest is not None else None,
        **base,
    }
    if closest is not None and distance is not None and distance < closest:
        return FocusReport(
            advice=Advice.TOO_CLOSE,
            advice_text=f"too close: the lens focuses from about {round_distance(closest):g} cm; move back",
            **report,
        )
    if detail is not None and detail >= GOOD_DETAIL_PX_PER_MM:
        return FocusReport(advice=Advice.GOOD, advice_text="good detail for small markings", **report)
    if distance is None and focus.distance_diopters is None:
        return FocusReport(advice=Advice.UNKNOWN, advice_text="no focus distance yet", **report)
    return FocusReport(advice=Advice.FAR, advice_text=closer_text(status.optics, closest), **report)


def closer_text(optics: Optics | None, closest: float | None) -> str:
    """For example "move closer: about 11 cm gives about 25 px/mm"."""
    if closest is None:
        return "move closer for more detail"
    target = closest * CLOSER_FACTOR
    detail = detail_px_per_mm(optics, target)
    gives = f" gives about {round_detail(detail):g} px/mm" if detail is not None else ""
    return f"move closer: about {round_distance(target):g} cm{gives}"


class PhoneStatusReport(CameraStatus):
    """The phone_status result: the camera status, and the distance and detail derived from it (flat)."""

    distance_cm: float | None = None
    detail_px_per_mm: float | None = None
    min_distance_cm: float | None = None
    focus_state: str | None = None
    calibration: str | None = None
    advice: Advice = Advice.UNKNOWN
    advice_text: str = ""
    sensor_zoom_boost: bool = False

    @classmethod
    def of(cls, status: CameraStatus) -> PhoneStatusReport:
        return cls(**status.model_dump(), **focus_report(status).model_dump())
