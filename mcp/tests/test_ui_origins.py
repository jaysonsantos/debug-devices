"""The page through an https tunnel (--ui-allowed-origin): off by default; with it, the Host and Origin checks accept
the exact origins, and every other origin is still refused. A browser write comes only from the page itself: a local
page on another port or scheme is refused (N94). Fakes only."""

import os
from pathlib import Path

import httpx
import pytest

from debug_devices_mcp.config import Settings
from debug_devices_mcp.ui.app import BAD_HOST, BAD_ORIGIN
from debug_devices_mcp.ui.constants import defaults as ui_defaults
from debug_devices_mcp.ui.monitor import Monitor, MonitorOptions
from debug_devices_mcp.ui.origins import PageOrigins, normalize_origin
from debug_devices_mcp.ui.settings import SettingsStore, UiSettings
from debug_devices_mcp.ui.setup import build_monitor
from debug_devices_mcp.webcam import Crop

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
    # A local page on another port or scheme, or the other local name, is not the page (N94).
    assert origins.from_the_page("http://127.0.0.1:18766", LOCAL_HOST)
    for other in (
        "http://localhost:8080",
        "https://127.0.0.1:18766",
        "http://127.0.0.1:5173",
        "http://localhost:18766",
    ):
        assert not origins.from_the_page(other, LOCAL_HOST), other


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


# region: N94 a local page on another port


def crop_monitor(tmp_path: Path, origins: tuple[str, ...]) -> Monitor:
    options = MonitorOptions(open_browser=False, port=0, allowed_origins=origins)
    monitor = Monitor(START, SettingsStore.in_dir(tmp_path), options)
    monitor.update_settings(UiSettings(webcam_crop=Crop(x=0, y=0, width=10, height=6)))
    return monitor


@pytest.mark.parametrize("origins", [(), (TUNNEL,)])
async def test_a_local_page_on_another_port_cannot_clear_the_crop(tmp_path: Path, origins: tuple[str, ...]) -> None:
    # dd-qa's probe: another local web page (a dev server, a local tool) must not clear the crop box, or the next
    # multimeter_read sends the whole frame to the vision model.
    monitor = crop_monitor(tmp_path, origins)
    async with page_client(monitor) as client:
        refused = [
            await client.delete("/api/settings/crop", headers={"Origin": other})
            for other in ("http://localhost:8080", "https://127.0.0.1:5173", "https://127.0.0.1:18766", "null")
        ]
    assert [response.status_code for response in refused] == [403, 403, 403, 403]
    assert refused[0].json()["error"] == BAD_ORIGIN.format(origin="http://localhost:8080", page=BASE_URL)
    assert SettingsStore.in_dir(tmp_path).load().webcam_crop is not None


async def test_the_page_and_a_local_process_can_still_clear_the_crop(tmp_path: Path) -> None:
    monitor = crop_monitor(tmp_path, ())
    async with page_client(monitor) as client:
        page = await client.delete("/api/settings/crop", headers={"Origin": BASE_URL})
        monitor.update_settings(UiSettings(webcam_crop=Crop(x=0, y=0, width=10, height=6)))
        # No Origin (a local process): as before.
        local = await client.delete("/api/settings/crop")
    assert (page.status_code, local.status_code) == (200, 200)
    assert SettingsStore.in_dir(tmp_path).load().webcam_crop is None


async def test_the_tunnel_origin_can_clear_the_crop_with_the_setting(tmp_path: Path) -> None:
    monitor = crop_monitor(tmp_path, (TUNNEL,))
    async with page_client(monitor) as client:
        response = await client.delete("/api/settings/crop", headers={"Host": LOCAL_HOST, "Origin": TUNNEL})
    assert response.status_code == 200
    assert SettingsStore.in_dir(tmp_path).load().webcam_crop is None


# endregion: N94 a local page on another port
