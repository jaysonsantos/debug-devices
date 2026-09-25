"""The phone preview gets the same flips as the snapshots: on a change, after connect, and after an app restart."""

import json
import logging
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from mcp import Client

from debug_devices_mcp.config import Settings
from debug_devices_mcp.images import SnapshotOrientation
from debug_devices_mcp.orientation import OrientationState
from debug_devices_mcp.phone_api import CameraStatus, PhoneClient, PreviewFlipRequest, PreviewNotSupportedError
from debug_devices_mcp.server import build_server
from debug_devices_mcp.ui.settings import SettingsStore

from .test_phone_api import STATUS
from .test_server import FakePhone, make_services, no_vision

REPO = Path(__file__).resolve().parents[2]
PREVIEW = "/v1/preview"
FAKE_PHONE_PORT = 18897
SERVER_START_SECONDS = 5


class PreviewPhone(FakePhone):
    """The httpx fake phone with POST /v1/preview. `supported=False` is an app from before it (404)."""

    def __init__(self, supported: bool = True) -> None:
        super().__init__()
        self.supported = supported
        self.status.update(preview_flip_horizontal=False, preview_flip_vertical=False)
        self.previews: list[dict[str, bool]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path != PREVIEW:
            return super().handle(request)
        self.requests.append((request.method, request.url.path))
        if not self.supported:
            return httpx.Response(404, json={"error": "not_found", "message": "no route"})
        body = json.loads(request.content)
        self.previews.append(body)
        self.status.update(preview_flip_horizontal=body["flip_horizontal"], preview_flip_vertical=body["flip_vertical"])
        return httpx.Response(200, json=self.status)

    def restart_app(self) -> None:
        self.status.update(preview_flip_horizontal=False, preview_flip_vertical=False)


def client_for(phone: FakePhone) -> PhoneClient:
    return PhoneClient(
        "http://phone", timedelta(seconds=1), timedelta(seconds=1), transport=httpx.MockTransport(phone.handle)
    )


async def test_client_sends_the_flips_and_reads_the_status() -> None:
    phone = PreviewPhone()
    status = await client_for(phone).preview(PreviewFlipRequest(flip_horizontal=True, flip_vertical=False))
    assert phone.previews == [{"flip_horizontal": True, "flip_vertical": False}]
    assert (status.preview_flip_horizontal, status.preview_flip_vertical) == (True, False)


async def test_client_reports_an_old_app() -> None:
    with pytest.raises(PreviewNotSupportedError, match="update the app"):
        await client_for(PreviewPhone(supported=False)).preview(
            PreviewFlipRequest(flip_horizontal=True, flip_vertical=True)
        )


def test_an_old_app_status_has_no_preview_fields() -> None:
    status = CameraStatus.model_validate(STATUS)  # the status of an app from before POST /v1/preview
    assert (status.preview_flip_horizontal, status.preview_flip_vertical) == (False, False)


def services_with(settings: Settings, phone: FakePhone, tmp_path: Path, flips: SnapshotOrientation):
    services = make_services(settings, phone, no_vision())
    services.orientation = OrientationState(SettingsStore.in_dir(tmp_path))
    services.orientation.update(flips.flip_horizontal, flips.flip_vertical)
    services.__post_init__()  # the sync uses the new orientation state
    return services


async def test_connect_toggle_and_restart_send_the_flips(settings: Settings, tmp_path: Path) -> None:
    phone = PreviewPhone()
    services = services_with(settings, phone, tmp_path, SnapshotOrientation(flip_vertical=True))
    async with Client(build_server(services)) as client:
        connected = await client.call_tool("phone_connect", {})
        assert connected.structured_content["status"]["preview_flip_vertical"] is True
        assert phone.previews == [{"flip_horizontal": False, "flip_vertical": True}]

        # The same flips: no new request.
        await client.call_tool("phone_status", {})
        assert len(phone.previews) == 1

        # A toggle (the page runs the same tool) sends the new flips at once.
        await client.call_tool("phone_snapshot_orientation", {"flip_horizontal": True})
        assert phone.previews[-1] == {"flip_horizontal": True, "flip_vertical": True}

        # The app restarts: its preview is not flipped. The next status read sends the flips again.
        phone.restart_app()
        status = await client.call_tool("phone_status", {})
        assert phone.previews[-1] == {"flip_horizontal": True, "flip_vertical": True}
        assert len(phone.previews) == 3
        assert status.structured_content["preview_flip_horizontal"] is True


async def test_an_old_app_gets_one_warning_and_the_snapshots_still_flip(
    settings: Settings, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    phone = PreviewPhone(supported=False)
    services = services_with(settings, phone, tmp_path, SnapshotOrientation(flip_horizontal=True))
    caplog.set_level(logging.WARNING)
    async with Client(build_server(services)) as client:
        assert not (await client.call_tool("phone_connect", {})).is_error
        for _ in range(3):
            assert not (await client.call_tool("phone_status", {})).is_error
        snapshot = await client.call_tool("phone_snapshot", {})
        assert not snapshot.is_error
        assert '"orientation":"flipped horizontally"' in snapshot.content[0].text
        # A new phone_connect tries again (the app can have an update).
        await client.call_tool("phone_connect", {})
    tries = [request for request in phone.requests if request[1] == PREVIEW]
    assert len(tries) == 2
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "preview" in r.getMessage()]
    assert len(warnings) == 2  # one per phone_connect, not one per status read


def test_the_fake_phone_passes_the_strict_contract() -> None:
    server = subprocess.Popen(
        [sys.executable, str(REPO / "scripts" / "fake_phone.py"), "--port", str(FAKE_PHONE_PORT)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + SERVER_START_SECONDS
        while time.monotonic() < deadline:
            try:
                httpx.get(f"http://127.0.0.1:{FAKE_PHONE_PORT}/v1/health", timeout=1)
                break
            except httpx.HTTPError:
                time.sleep(0.1)
        result = subprocess.run(
            [
                sys.executable,
                str(REPO / "scripts" / "qa_contract.py"),
                "--base-url",
                f"http://127.0.0.1:{FAKE_PHONE_PORT}",
                "--strict",
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=60,
        )
    finally:
        server.terminate()
        server.wait()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS preview" in result.stdout
