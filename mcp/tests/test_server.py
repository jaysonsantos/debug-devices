import base64
import json
from datetime import timedelta
from pathlib import Path

import httpx
from mcp import Client
from mcp.types import ImageContent, TextContent
from pydantic import SecretStr

from debug_devices_mcp.adb import Adb
from debug_devices_mcp.config import Settings
from debug_devices_mcp.multimeter import VisionClient
from debug_devices_mcp.phone_api import PhoneClient
from debug_devices_mcp.process import CommandResult
from debug_devices_mcp.server import Services, build_server
from debug_devices_mcp.webcam import Webcam

from .conftest import JPEG, FakeRunner, ok
from .test_multimeter import READING, completion
from .test_phone_api import STATUS

ONE_DEVICE = b"List of devices attached\nR5CT1234567  device usb:1-2 model:SM_A556B transport_id:3\n"


class FakePhone:
    """In-memory phone app behind an httpx MockTransport. `up` is False until `am start`."""

    def __init__(self, up: bool = True) -> None:
        self.up = up
        self.status = dict(STATUS)
        self.requests: list[tuple[str, str]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path))
        if not self.up:
            raise httpx.ConnectError("refused", request=request)
        match request.url.path:
            case "/v1/health":
                return httpx.Response(200, json={"ok": True, "app_version": "0.1.0"})
            case "/v1/status":
                return httpx.Response(200, json=self.status)
            case "/v1/zoom":
                body = json.loads(request.content)
                ratio = body.get("ratio", self.status["zoom_ratio"] * 1.5)
                self.status["zoom_ratio"] = min(ratio, self.status["max_zoom_ratio"])
                return httpx.Response(200, json=self.status)
            case "/v1/torch":
                self.status["torch_enabled"] = json.loads(request.content)["enabled"]
                return httpx.Response(200, json=self.status)
            case "/v1/snapshot":
                return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})
        return httpx.Response(404, json={"error": "not_found", "message": "no route"})


def make_services(settings: Settings, fake_phone: FakePhone, vision_handler: httpx.MockTransport) -> Services:
    def respond(command: list[str]) -> CommandResult:
        if command[0] == "ffmpeg":
            return ok(JPEG)
        if "am" in command:
            fake_phone.up = True
            return ok(b"Starting: Intent\n")
        if command[1:] == ["devices", "-l"]:
            return ok(ONE_DEVICE)
        return ok()

    runner = FakeRunner(respond)
    return Services(
        settings=settings,
        adb=Adb(runner, "adb", timedelta(seconds=1)),
        phone=PhoneClient(
            "http://phone", timedelta(seconds=1), timedelta(seconds=1), transport=httpx.MockTransport(fake_phone.handle)
        ),
        webcam=Webcam(runner, "ffmpeg", Path("/dev/video0"), 0, timedelta(seconds=1)),
        vision=VisionClient(
            SecretStr("sk-test"), "m", "https://openrouter.test/api/v1", timedelta(seconds=1), transport=vision_handler
        ),
    )


def no_vision() -> httpx.MockTransport:
    return httpx.MockTransport(lambda request: completion(json.dumps(READING)))


async def test_connect_starts_app_when_down(settings: Settings) -> None:
    fake_phone = FakePhone(up=False)
    services = make_services(settings, fake_phone, no_vision())

    async with Client(build_server(services)) as client:
        result = await client.call_tool("phone_connect", {})

    assert not result.is_error, result.content
    assert result.structured_content is not None
    assert result.structured_content["serial"] == "R5CT1234567"
    assert result.structured_content["started_app"] is True
    assert result.structured_content["status"]["max_zoom_ratio"] == 8.0


async def test_zoom_torch_and_snapshot(settings: Settings, tmp_path: Path) -> None:
    fake_phone = FakePhone()
    services = make_services(settings, fake_phone, no_vision())
    target = tmp_path / "shots" / "phone.jpg"

    async with Client(build_server(services)) as client:
        zoom = await client.call_tool("phone_zoom", {"step": "in"})
        torch = await client.call_tool("phone_torch", {"enabled": True})
        snapshot = await client.call_tool("phone_snapshot", {"save_path": str(target)})
        both = await client.call_tool("phone_zoom", {"ratio": 2, "step": "in"})

    assert zoom.structured_content is not None
    assert zoom.structured_content["zoom_ratio"] == 1.5
    assert torch.structured_content is not None
    assert torch.structured_content["torch_enabled"] is True
    image = next(block for block in snapshot.content if isinstance(block, ImageContent))
    assert image.mime_type == "image/jpeg"
    assert base64.b64decode(image.data) == JPEG
    assert target.read_bytes() == JPEG
    assert both.is_error


async def test_phone_not_connected_hint(settings: Settings) -> None:
    services = make_services(settings, FakePhone(up=False), no_vision())

    async with Client(build_server(services)) as client:
        result = await client.call_tool("phone_status", {})

    assert result.is_error
    text = next(block for block in result.content if isinstance(block, TextContent)).text
    assert "phone_connect" in text


async def test_multimeter_read(settings: Settings) -> None:
    services = make_services(settings, FakePhone(), no_vision())

    async with Client(build_server(services)) as client:
        plain = await client.call_tool("multimeter_read", {})
        with_image = await client.call_tool("multimeter_read", {"include_image": True})

    assert not plain.is_error, plain.content
    assert plain.structured_content == READING
    assert not any(isinstance(block, ImageContent) for block in plain.content)
    assert any(isinstance(block, ImageContent) for block in with_image.content)


async def test_multimeter_without_key(settings: Settings) -> None:
    services = make_services(settings, FakePhone(), no_vision())
    services.vision = VisionClient(None, "m", "https://openrouter.test/api/v1", timedelta(seconds=1))

    async with Client(build_server(services)) as client:
        result = await client.call_tool("multimeter_read", {})

    assert result.is_error
    text = next(block for block in result.content if isinstance(block, TextContent)).text
    assert "OPENROUTER_API_KEY is not set" in text


async def test_webcam_snapshot(settings: Settings) -> None:
    services = make_services(settings, FakePhone(), no_vision())

    async with Client(build_server(services)) as client:
        result = await client.call_tool("webcam_snapshot", {})

    assert not result.is_error
    assert any(isinstance(block, ImageContent) for block in result.content)
