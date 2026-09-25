"""Typed HTTP client for the phone camera API in docs/phone-api.md."""

from datetime import timedelta
from enum import StrEnum
from http import HTTPMethod, HTTPStatus
from typing import Annotated, Literal

import httpx
from pydantic import BaseModel, Field, ValidationError, model_validator

from debug_devices_mcp.constants import (
    CONTENT_TYPE_HEADER,
    ERROR_BODY_PREVIEW_CHARS,
    JSON_CONTENT_TYPE,
    defaults,
    phone,
)

# region: models


class Health(BaseModel):
    ok: bool
    app_version: str


# Snapshot rotation in degrees, as the contract allows it.
type RotationDegrees = Literal[0, 90, 180, 270]


class FocusState(StrEnum):
    FOCUSED = "focused"
    SCANNING = "scanning"
    UNFOCUSED = "unfocused"
    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls, value: object) -> FocusState:
        return cls.UNKNOWN


class FocusCalibration(StrEnum):
    """`LENS_INFO_FOCUS_DISTANCE_CALIBRATION`. With `uncalibrated`, the distance is not in real units."""

    UNCALIBRATED = "uncalibrated"
    APPROXIMATE = "approximate"
    CALIBRATED = "calibrated"

    @classmethod
    def _missing_(cls, value: object) -> FocusCalibration:
        return cls.UNCALIBRATED


class Focus(BaseModel):
    # 1/m; 0 means infinity; null before the first result or with a fixed-focus lens.
    distance_diopters: float | None = None
    state: FocusState = FocusState.UNKNOWN
    calibration: FocusCalibration = FocusCalibration.UNCALIBRATED
    # 0 means fixed focus.
    min_distance_diopters: float = 0.0


class Optics(BaseModel):
    """The camera of /v1/snapshot: focal length, physical sensor width, and snapshot width before rotation."""

    focal_length_mm: float
    sensor_width_mm: float
    output_width_px: int


class InSensorZoom(StrEnum):
    """The vendor in-sensor zoom: real extra detail at 2x or more from a sensor crop, on phones that have it."""

    OFF = "off"
    ON = "on"
    # The phone has no such vendor mode.
    UNSUPPORTED = "unsupported"
    # The vendor session failed; the app runs in the normal mode.
    FALLBACK = "fallback"

    @classmethod
    def _missing_(cls, value: object) -> InSensorZoom:
        return cls.OFF


class CameraStatus(BaseModel):
    zoom_ratio: float
    min_zoom_ratio: float
    max_zoom_ratio: float
    torch_enabled: bool
    has_flash_unit: bool
    rotation_degrees: RotationDegrees
    rotation_locked: bool
    # The camera preview flips on the phone screen. An app from before POST /v1/preview does not send them.
    preview_flip_horizontal: bool = False
    preview_flip_vertical: bool = False
    # Also newer than the first app: None when the app does not send them (or before the camera is bound).
    focus: Focus | None = None
    optics: Optics | None = None
    # None: an app from before POST /v1/camera.
    in_sensor_zoom: InSensorZoom | None = None
    # The number of highlight boxes on the phone screen. None: an app from before POST /v1/overlay.
    overlay_boxes: int | None = None


class ApiErrorCode(StrEnum):
    CAMERA_NOT_READY = "camera_not_ready"
    NO_FLASH_UNIT = "no_flash_unit"
    BAD_REQUEST = "bad_request"
    NOT_FOUND = "not_found"
    METHOD_NOT_ALLOWED = "method_not_allowed"
    CAPTURE_FAILED = "capture_failed"
    INTERNAL_ERROR = "internal_error"


class ApiError(BaseModel):
    error: ApiErrorCode | str
    message: str


class ZoomStep(StrEnum):
    IN = "in"
    OUT = "out"


class ZoomRatioRequest(BaseModel):
    ratio: float


class ZoomStepRequest(BaseModel):
    step: ZoomStep


class TorchRequest(BaseModel):
    enabled: bool


class RotationLockRequest(BaseModel):
    degrees: RotationDegrees


class RotationAutoRequest(BaseModel):
    auto: Literal[True] = True


# A point in [0, 1] on the phone screen or on the snapshot, 0,0 = top left.
# Float rounding of a box that ends on the image edge (x + width can be 1.0000000001).
BOX_TOLERANCE = 1e-9
type UnitCoordinate = Annotated[float, Field(ge=0, le=1)]


class ScreenFocusRequest(BaseModel):
    """A point on the phone screen as the screen stream shows it (natural portrait orientation)."""

    screen_x: UnitCoordinate
    screen_y: UnitCoordinate


class SnapshotFocusRequest(BaseModel):
    """A point on the image of /v1/snapshot now (true orientation, before the server flips)."""

    snapshot_x: UnitCoordinate
    snapshot_y: UnitCoordinate


class OverlayBox(BaseModel):
    """A highlight box on the image of /v1/snapshot now (true orientation), from 0 to 1, inside the image."""

    snapshot_x: UnitCoordinate
    snapshot_y: UnitCoordinate
    width: Annotated[float, Field(gt=0, le=1)]
    height: Annotated[float, Field(gt=0, le=1)]
    label: Annotated[str, Field(max_length=phone.OVERLAY_MAX_LABEL)] = ""

    @model_validator(mode="after")
    def _inside(self) -> OverlayBox:
        if self.snapshot_x + self.width > 1 + BOX_TOLERANCE or self.snapshot_y + self.height > 1 + BOX_TOLERANCE:
            raise ValueError("the box must be inside the image")
        return self


class OverlayRequest(BaseModel):
    boxes: Annotated[list[OverlayBox], Field(max_length=phone.OVERLAY_MAX_BOXES)]


class CameraSettingsRequest(BaseModel):
    in_sensor_zoom: bool


class PreviewFlipRequest(BaseModel):
    flip_horizontal: bool
    flip_vertical: bool


# endregion: models

# region: errors


class PhoneError(Exception):
    """Base error of the phone client."""


class PhoneUnreachableError(PhoneError):
    """No HTTP server answers on the forwarded port."""


class PhoneApiError(PhoneError):
    """The app answered with a non-2xx status and an `ApiError` body."""

    def __init__(self, status: int, error: ApiError) -> None:
        super().__init__(f"phone API error {status} {error.error}: {error.message}")
        self.status = status
        self.error = error


class PreviewNotSupportedError(PhoneError):
    """The app has no POST /v1/preview (an app from before it): 404."""


class CameraSettingsNotSupportedError(PhoneError):
    """The app has no POST /v1/camera (an app from before it): 404."""


class FocusNotSupportedError(PhoneError):
    """The app has no POST /v1/focus (an app from before it): 404."""


class OverlayNotSupportedError(PhoneError):
    """The app has no POST /v1/overlay (an app from before it): 404."""


class PhoneProtocolError(PhoneError):
    """The app answered with a body that does not match the contract."""


# endregion: errors


class PhoneClient:
    """One client per forwarded local port. `base_url` is `http://127.0.0.1:<local>`."""

    def __init__(
        self,
        base_url: str,
        timeout: timedelta,
        snapshot_timeout: timedelta,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._snapshot_timeout = snapshot_timeout
        self._http = httpx.AsyncClient(base_url=base_url, timeout=timeout.total_seconds(), transport=transport)

    @classmethod
    def for_local_port(cls, port: int, timeout: timedelta, snapshot_timeout: timedelta) -> PhoneClient:
        return cls(f"http://{phone.HOST}:{port}", timeout, snapshot_timeout)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def health(self) -> Health:
        return _parse(Health, phone.PATH_HEALTH, await self._request(HTTPMethod.GET, phone.PATH_HEALTH))

    async def status(self) -> CameraStatus:
        return _parse(CameraStatus, phone.PATH_STATUS, await self._request(HTTPMethod.GET, phone.PATH_STATUS))

    async def zoom(self, request: ZoomRatioRequest | ZoomStepRequest) -> CameraStatus:
        body = await self._request(HTTPMethod.POST, phone.PATH_ZOOM, request)
        return _parse(CameraStatus, phone.PATH_ZOOM, body)

    async def torch(self, enabled: bool) -> CameraStatus:
        body = await self._request(HTTPMethod.POST, phone.PATH_TORCH, TorchRequest(enabled=enabled))
        return _parse(CameraStatus, phone.PATH_TORCH, body)

    async def rotation(self, request: RotationLockRequest | RotationAutoRequest) -> CameraStatus:
        body = await self._request(HTTPMethod.POST, phone.PATH_ROTATION, request)
        return _parse(CameraStatus, phone.PATH_ROTATION, body)

    async def preview(self, request: PreviewFlipRequest) -> CameraStatus:
        """Mirror the camera preview on the phone screen. The snapshot stays in the true orientation."""
        try:
            body = await self._request(HTTPMethod.POST, phone.PATH_PREVIEW, request)
        except PhoneApiError as exc:
            if exc.status == HTTPStatus.NOT_FOUND:
                raise PreviewNotSupportedError(f"the phone app has no {phone.PATH_PREVIEW}; update the app") from exc
            raise
        return _parse(CameraStatus, phone.PATH_PREVIEW, body)

    async def camera(self, request: CameraSettingsRequest) -> CameraStatus:
        """Turn the in-sensor zoom on or off. The app binds the camera again: on takes about 5 s, off about 1 s."""
        try:
            body = await self._request(
                HTTPMethod.POST, phone.PATH_CAMERA, request, timeout=defaults.PHONE_RECONFIGURE_TIMEOUT
            )
        except PhoneApiError as exc:
            if exc.status == HTTPStatus.NOT_FOUND:
                raise CameraSettingsNotSupportedError(
                    f"the phone app has no {phone.PATH_CAMERA}; update the phone app to use the in-sensor zoom"
                ) from exc
            raise
        return _parse(CameraStatus, phone.PATH_CAMERA, body)

    async def focus(self, request: ScreenFocusRequest | SnapshotFocusRequest) -> CameraStatus:
        """Focus and meter on one point. The answer comes at once; later statuses show scanning, then focused."""
        try:
            body = await self._request(HTTPMethod.POST, phone.PATH_FOCUS, request)
        except PhoneApiError as exc:
            if exc.status == HTTPStatus.NOT_FOUND:
                raise FocusNotSupportedError(
                    f"the phone app has no {phone.PATH_FOCUS}; update the phone app to focus on a point"
                ) from exc
            raise
        return _parse(CameraStatus, phone.PATH_FOCUS, body)

    async def overlay(self, request: OverlayRequest) -> CameraStatus:
        """Draw highlight boxes over the camera preview on the phone screen. No boxes: remove them."""
        try:
            body = await self._request(HTTPMethod.POST, phone.PATH_OVERLAY, request)
        except PhoneApiError as exc:
            if exc.status == HTTPStatus.NOT_FOUND:
                raise OverlayNotSupportedError(
                    f"the phone app has no {phone.PATH_OVERLAY}; update the phone app to show highlight boxes"
                ) from exc
            raise
        return _parse(CameraStatus, phone.PATH_OVERLAY, body)

    async def snapshot(self) -> bytes:
        return await self._request(HTTPMethod.GET, phone.PATH_SNAPSHOT, timeout=self._snapshot_timeout)

    async def _request(
        self, method: HTTPMethod, path: str, body: BaseModel | None = None, timeout: timedelta | None = None
    ) -> bytes:
        content = body.model_dump_json().encode() if body is not None else None
        headers = {CONTENT_TYPE_HEADER: JSON_CONTENT_TYPE} if body is not None else None
        extra = {"timeout": timeout.total_seconds()} if timeout is not None else {}
        try:
            response = await self._http.request(method, path, content=content, headers=headers, **extra)
        except httpx.TransportError as exc:
            raise PhoneUnreachableError(f"phone API at {self._http.base_url} is unreachable: {exc!r}") from exc
        if response.status_code >= HTTPStatus.MULTIPLE_CHOICES:
            preview = response.text.strip()[:ERROR_BODY_PREVIEW_CHARS]
            try:
                error = ApiError.model_validate_json(response.content)
            except ValidationError as exc:
                raise PhoneProtocolError(
                    f"phone API returned {response.status_code} without an ApiError body: {preview!r}"
                ) from exc
            raise PhoneApiError(response.status_code, error)
        return response.content


def _parse[T: BaseModel](model: type[T], path: str, body: bytes) -> T:
    """A 2xx body that does not match the contract is a `PhoneProtocolError`, not a bare `ValidationError`."""
    try:
        return model.model_validate_json(body)
    except ValidationError as exc:
        preview = body.decode(errors="replace").strip()[:ERROR_BODY_PREVIEW_CHARS]
        raise PhoneProtocolError(
            f"phone API {path} returned a body that is not a {model.__name__}: {preview!r}"
        ) from exc
