import json
from datetime import timedelta

import httpx
import pytest

from debug_devices_mcp.phone_api import (
    ApiError,
    ApiErrorCode,
    CameraStatus,
    PhoneApiError,
    PhoneClient,
    PhoneProtocolError,
    PhoneUnreachableError,
    ZoomRatioRequest,
    ZoomStep,
    ZoomStepRequest,
)

from .conftest import JPEG

STATUS = {
    "zoom_ratio": 1.0,
    "min_zoom_ratio": 1.0,
    "max_zoom_ratio": 8.0,
    "torch_enabled": False,
    "has_flash_unit": True,
}


def client(handler: httpx.MockTransport) -> PhoneClient:
    return PhoneClient("http://phone", timedelta(seconds=1), timedelta(seconds=1), transport=handler)


async def test_status_and_health() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/health":
            return httpx.Response(200, json={"ok": True, "app_version": "0.1.0"})
        assert request.url.path == "/v1/status"
        return httpx.Response(200, json=STATUS)

    phone = client(httpx.MockTransport(handler))
    assert (await phone.health()).app_version == "0.1.0"
    status = await phone.status()
    assert status.max_zoom_ratio == 8.0
    assert status.has_flash_unit


@pytest.mark.parametrize(
    ("request_model", "body"),
    [(ZoomRatioRequest(ratio=2.5), {"ratio": 2.5}), (ZoomStepRequest(step=ZoomStep.IN), {"step": "in"})],
)
async def test_zoom_body(request_model: ZoomRatioRequest | ZoomStepRequest, body: dict[str, object]) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={**STATUS, "zoom_ratio": 2.5})

    status = await client(httpx.MockTransport(handler)).zoom(request_model)

    assert status.zoom_ratio == 2.5
    assert seen[0].method == "POST"
    assert seen[0].url.path == "/v1/zoom"
    assert seen[0].headers["content-type"] == "application/json"
    assert json.loads(seen[0].content) == body


async def test_torch_error_is_typed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {"enabled": True}
        return httpx.Response(409, json={"error": "no_flash_unit", "message": "No flash"})

    with pytest.raises(PhoneApiError) as caught:
        await client(httpx.MockTransport(handler)).torch(True)

    assert caught.value.status == 409
    assert caught.value.error.error == ApiErrorCode.NO_FLASH_UNIT


async def test_snapshot_returns_bytes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})

    assert await client(httpx.MockTransport(handler)).snapshot() == JPEG


async def test_error_without_api_error_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with pytest.raises(PhoneProtocolError):
        await client(httpx.MockTransport(handler)).status()


async def test_unreachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(PhoneUnreachableError):
        await client(httpx.MockTransport(handler)).health()


def test_contract_examples_parse() -> None:
    status = CameraStatus.model_validate_json(
        '{"zoom_ratio": 1.0, "min_zoom_ratio": 1.0, "max_zoom_ratio": 8.0, "torch_enabled": false,'
        ' "has_flash_unit": true}'
    )
    error = ApiError.model_validate_json('{"error": "camera_not_ready", "message": "Camera is not bound yet"}')

    assert status.max_zoom_ratio == 8.0
    assert error.error == ApiErrorCode.CAMERA_NOT_READY
    assert "camera_not_ready" in str(PhoneApiError(503, error))
