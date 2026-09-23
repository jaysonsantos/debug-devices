"""Typed HTTP client for the phone camera API in docs/phone-api.md."""

from datetime import timedelta
from enum import StrEnum
from http import HTTPMethod, HTTPStatus

import httpx
from pydantic import BaseModel, ValidationError

from debug_devices_mcp.constants import CONTENT_TYPE_HEADER, ERROR_BODY_PREVIEW_CHARS, JSON_CONTENT_TYPE, phone

# region: models


class Health(BaseModel):
    ok: bool
    app_version: str


class CameraStatus(BaseModel):
    zoom_ratio: float
    min_zoom_ratio: float
    max_zoom_ratio: float
    torch_enabled: bool
    has_flash_unit: bool


class ApiErrorCode(StrEnum):
    CAMERA_NOT_READY = "camera_not_ready"
    NO_FLASH_UNIT = "no_flash_unit"
    BAD_REQUEST = "bad_request"
    NOT_FOUND = "not_found"
    CAPTURE_FAILED = "capture_failed"


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
        return Health.model_validate_json(await self._request(HTTPMethod.GET, phone.PATH_HEALTH))

    async def status(self) -> CameraStatus:
        return CameraStatus.model_validate_json(await self._request(HTTPMethod.GET, phone.PATH_STATUS))

    async def zoom(self, request: ZoomRatioRequest | ZoomStepRequest) -> CameraStatus:
        return CameraStatus.model_validate_json(await self._request(HTTPMethod.POST, phone.PATH_ZOOM, request))

    async def torch(self, enabled: bool) -> CameraStatus:
        body = TorchRequest(enabled=enabled)
        return CameraStatus.model_validate_json(await self._request(HTTPMethod.POST, phone.PATH_TORCH, body))

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
            preview = response.text[:ERROR_BODY_PREVIEW_CHARS]
            try:
                error = ApiError.model_validate_json(response.content)
            except ValidationError as exc:
                raise PhoneProtocolError(
                    f"phone API returned {response.status_code} without an ApiError body: {preview!r}"
                ) from exc
            raise PhoneApiError(response.status_code, error)
        return response.content
