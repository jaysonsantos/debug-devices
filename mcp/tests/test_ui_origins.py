"""The page through an https tunnel (--ui-allowed-origin): off by default; with it, the Host and Origin checks accept
the exact origins, and every other origin is still refused. Fakes only."""

import os
from pathlib import Path

import httpx
import pytest

from debug_devices_mcp.config import Settings
from debug_devices_mcp.ui.app import BAD_HOST
from debug_devices_mcp.ui.constants import defaults as ui_defaults
from debug_devices_mcp.ui.monitor import Monitor, MonitorOptions
from debug_devices_mcp.ui.origins import PageOrigins, normalize_origin
from debug_devices_mcp.ui.settings import SettingsStore
from debug_devices_mcp.ui.setup import build_monitor

from .conftest import free_port
from .test_markings import START
from .test_server import FakePhone, make_services, no_vision
from .test_staged_capture import BASE_URL, capture_setup, page_client

TUNNEL = "https://bench.example.org"
TUNNEL_HOST = "bench.example.org"
LOCAL_HOST = "127.0.0.1:18766"


def tunnel_monitor(settings: Settings, tmp_path: Path, origins: tuple[str, ...]) -> Monitor:
    capturer, _, _, _ = capture_setup(settings, tmp_path)
    options = MonitorOptions(open_browser=False, port=0, allowed_origins=origins)
    monitor = Monitor(START, SettingsStore.in_dir(tmp_path), options)
    monitor.staged = capturer
    return monitor


async def post_capture(client: httpx.AsyncClient, host: str, origin: str) -> httpx.Response:
    return await client.post("/api/staged", headers={"Host": host, "Origin": origin})


# region: the setting


def test_the_setting_is_off_by_default_and_takes_exact_origins(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings.from_cli([]).ui_allowed_origins == []
    flags = ["--ui-allowed-origin", "https://Bench.Example.org/", "--ui-allowed-origin", "http://192.0.2.5:8080"]
    assert Settings.from_cli(flags).ui_allowed_origins == [TUNNEL, "http://192.0.2.5:8080"]
    monkeypatch.setenv("DEBUG_DEVICES_UI_ALLOWED_ORIGINS", f"{TUNNEL}, https://other.example.org:443")
    assert Settings.from_cli([]).ui_allowed_origins == [TUNNEL, "https://other.example.org"]
    # Not an origin (no scheme, a path): refused at start.
    for bad in ("bench.example.org", "https://bench.example.org/page", "ftp://bench.example.org"):
        with pytest.raises(ValueError, match="is not an origin"):
            normalize_origin(bad)


def test_build_monitor_passes_the_origins_to_the_page(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", os.fspath(tmp_path))
    # Free ports, never the user's 18766 (as in test_build_monitor_takes_over_the_webcam_and_the_model).
    monkeypatch.setattr(ui_defaults, "PORT", free_port())
    tunnel = settings.model_copy(update={"ui_allowed_origins": [TUNNEL], "ui_port": free_port()})
    monitor = build_monitor(tunnel, make_services(tunnel, FakePhone(), no_vision()))
    assert monitor.options.allowed_origins == (TUNNEL,)


def test_page_origins() -> None:
    origins = PageOrigins.of([TUNNEL])
    assert origins.host_allowed(TUNNEL_HOST)
    assert origins.host_allowed(LOCAL_HOST)
    assert not origins.host_allowed("evil.example")
    # The Host of the tunnel, or the Host that the tunnel wrote (127.0.0.1): the exact origin decides.
    assert origins.from_the_page(TUNNEL, TUNNEL_HOST)
    assert origins.from_the_page(TUNNEL, LOCAL_HOST)
    assert not origins.from_the_page("http://bench.example.org", LOCAL_HOST)
    assert not origins.from_the_page("https://bench.example.org:8443", LOCAL_HOST)
    assert not origins.from_the_page(None, TUNNEL_HOST)


# endregion: the setting

# region: the checks


async def test_without_the_setting_the_tunnel_is_refused_as_before(settings: Settings, tmp_path: Path) -> None:
    monitor = tunnel_monitor(settings, tmp_path, ())
    async with page_client(monitor) as client:
        page = await client.get("/api/staged", headers={"Host": TUNNEL_HOST})
        # A tunnel that writes Host 127.0.0.1: the page loads, but a capture from its origin is refused.
        rewritten = await post_capture(client, LOCAL_HOST, TUNNEL)
        tunnel_host = await post_capture(client, TUNNEL_HOST, TUNNEL)
    assert (page.status_code, page.json()["error"]) == (403, BAD_HOST)
    assert rewritten.status_code == 403
    # The page shows the reason (JSON, like every other page error).
    assert "--ui-allowed-origin" in rewritten.json()["error"]
    assert tunnel_host.status_code == 403
    assert await monitor.staged.store.list() == []


async def test_with_the_setting_the_tunnel_origin_can_use_the_page(settings: Settings, tmp_path: Path) -> None:
    monitor = tunnel_monitor(settings, tmp_path, (TUNNEL,))
    async with page_client(monitor) as client:
        page = await client.get("/", headers={"Host": TUNNEL_HOST})
        listed = await client.get("/api/staged", headers={"Host": TUNNEL_HOST})
        # The tunnel keeps the Host of the browser, or writes 127.0.0.1: both pass with the exact origin.
        kept = await post_capture(client, TUNNEL_HOST, TUNNEL)
        rewritten = await post_capture(client, LOCAL_HOST, TUNNEL)
        # Another origin is still refused: another site, another scheme, another port, another host.
        others = [
            await post_capture(client, LOCAL_HOST, "https://evil.example"),
            await post_capture(client, TUNNEL_HOST, "http://bench.example.org"),
            await post_capture(client, LOCAL_HOST, "https://bench.example.org:8443"),
            await client.get("/api/staged", headers={"Host": "evil.example"}),
        ]
        # A local process without Origin is still not the page.
        no_origin = await client.post("/api/staged")
        await monitor.staged.wait()
    assert (page.status_code, listed.status_code) == (200, 200)
    assert (kept.status_code, rewritten.status_code) == (202, 202)
    assert [response.status_code for response in others] == [403, 403, 403, 403]
    assert no_origin.status_code == 403
    assert len(await monitor.staged.store.list()) == 2


async def test_the_local_page_still_works_with_the_setting(settings: Settings, tmp_path: Path) -> None:
    monitor = tunnel_monitor(settings, tmp_path, (TUNNEL,))
    async with page_client(monitor) as client:
        created = await client.post("/api/staged", headers={"Origin": BASE_URL})
        await monitor.staged.wait()
    assert created.status_code == 202


# endregion: the checks
