"""Find wireless-debugging phones without adb mDNS: python-zeroconf (a fake service browser), avahi-browse (a
recorded sample with invented names and 192.0.2.x addresses), the fallback order, the cache, and phone_devices."""

import json
from datetime import timedelta
from pathlib import Path
from typing import Any, ClassVar

import pytest
from mcp import Client
from starlette.testclient import TestClient
from zeroconf import IPVersion, ServiceStateChange

from debug_devices_mcp.adb import AdbDevice
from debug_devices_mcp.discovery import (
    AVAHI_TIMEOUT,
    NOTHING_FOUND,
    AvahiSource,
    CandidateKind,
    DiscoveryError,
    DiscoverySource,
    FoundService,
    PhoneDiscovery,
    ZeroconfSource,
    matching_serial,
    parse_avahi,
)
from debug_devices_mcp.process import CommandError, CommandResult
from debug_devices_mcp.ui.app import create_app

from .conftest import FakeRunner, ok
from .test_devices import PHONE, TV, Bench, bench  # noqa: F401

CONNECT_TYPE = "_adb-tls-connect._tcp.local."
PAIRING_TYPE = "_adb-tls-pairing._tcp.local."
CONNECT_NAME = "adb-R5CT1234567-Ab12Cd"
PAIRING_NAME = "adb-R5CT1234567-Ef34Gh"
HOST = "192.0.2.23"
CONNECT_PORT = 41023
PAIRING_PORT = 37099

# region: zeroconf


class FakeInfo:
    """AsyncServiceInfo: resolves the services in `answers` (full name -> (addresses, port, properties))."""

    answers: ClassVar[dict[str, tuple[list[str], int, dict[bytes, bytes | None]]]] = {}

    def __init__(self, service_type: str, name: str) -> None:
        self.service_type, self.name = service_type, name
        self.port = 0
        self.properties: dict[bytes, bytes | None] = {}
        self._addresses: list[str] = []

    async def async_request(self, zc: Any, timeout: float) -> bool:
        answer = self.answers.get(self.name)
        if answer is None:
            return False
        self._addresses, self.port, self.properties = answer
        return True

    def parsed_addresses(self, version: IPVersion) -> list[str]:
        assert version is IPVersion.V4Only
        return self._addresses


class FakeZeroconf:
    closed = False

    def __init__(self, ip_version: IPVersion) -> None:
        self.zeroconf = object()

    async def async_close(self) -> None:
        FakeZeroconf.closed = True


def fake_browser(announced: list[tuple[str, str]]):
    """A service browser that announces `announced` (type, full name) at once."""

    class FakeBrowser:
        cancelled = False

        def __init__(self, zeroconf: Any, types: list[str], handlers: list) -> None:
            assert sorted(types) == sorted([CONNECT_TYPE, PAIRING_TYPE])
            for service_type, name in announced:
                for handler in handlers:
                    handler(
                        zeroconf=zeroconf, service_type=service_type, name=name, state_change=ServiceStateChange.Added
                    )

        async def async_cancel(self) -> None:
            FakeBrowser.cancelled = True

    return FakeBrowser


async def test_zeroconf_finds_and_resolves_both_kinds() -> None:
    connect, pairing, silent = (
        f"{CONNECT_NAME}.{CONNECT_TYPE}",
        f"{PAIRING_NAME}.{PAIRING_TYPE}",
        f"adb-OTHER-x.{CONNECT_TYPE}",
    )
    FakeInfo.answers = {
        connect: ([HOST], CONNECT_PORT, {b"name": b"SM-A556B", b"api": b"36"}),
        pairing: ([HOST], PAIRING_PORT, {}),
    }
    browser = fake_browser([(CONNECT_TYPE, connect), (PAIRING_TYPE, pairing), (CONNECT_TYPE, silent)])
    FakeZeroconf.closed = False
    source = ZeroconfSource(FakeZeroconf, browser, FakeInfo, browse_time=timedelta(0))
    found = await source.browse()
    assert {(service.name, service.kind, service.address, service.model) for service in found} == {
        (CONNECT_NAME, CandidateKind.CONNECT, f"{HOST}:{CONNECT_PORT}", "SM-A556B"),
        (PAIRING_NAME, CandidateKind.PAIRING, f"{HOST}:{PAIRING_PORT}", ""),
    }
    # The browser and zeroconf are closed after the browse.
    assert browser.cancelled
    assert FakeZeroconf.closed


async def test_zeroconf_that_cannot_start_is_a_discovery_error() -> None:
    def broken(ip_version: IPVersion) -> None:
        raise OSError("no multicast interface")

    with pytest.raises(DiscoveryError, match="no multicast interface"):
        await ZeroconfSource(broken, fake_browser([]), FakeInfo, browse_time=timedelta(0)).browse()


# endregion: zeroconf

# region: avahi-browse

# A recorded `avahi-browse -rtp _adb-tls-connect._tcp`, with invented names and addresses. The IPv6 line with an
# IPv4 address is real behaviour of the bench PC; the fe80 line is an IPv6 address (not listed).
RESOLVED = "=;enp5s0;{proto};adb-R5CT1234567-Ab12Cd;_adb-tls-connect._tcp;local;Android-2.local;{address};41023;{txt}"
TXT = '"api=36.0" "name=SM-A556B" "v=1"'
AVAHI_SAMPLE = "\n".join(
    [
        "+;enp5s0;IPv6;adb-R5CT1234567-Ab12Cd;_adb-tls-connect._tcp;local",
        "+;enp5s0;IPv4;adb-R5CT1234567-Ab12Cd;_adb-tls-connect._tcp;local",
        RESOLVED.format(proto="IPv6", address=HOST, txt=TXT),
        RESOLVED.format(proto="IPv4", address=HOST, txt=TXT),
        RESOLVED.format(proto="IPv6", address="fe80::1", txt='"v=1"'),
        r"=;wlan0;IPv4;Lab\032phone;_adb-tls-connect._tcp;local;lab.local;192.0.2.77;35111;",
        "",
    ]
)


def test_avahi_output_is_parsed() -> None:
    found = parse_avahi(AVAHI_SAMPLE, CandidateKind.CONNECT)
    assert [(service.name, service.address, service.model) for service in found] == [
        (CONNECT_NAME, f"{HOST}:{CONNECT_PORT}", "SM-A556B"),
        ("Lab phone", "192.0.2.77:35111", ""),
    ]
    assert parse_avahi("", CandidateKind.PAIRING) == []


async def test_avahi_browses_both_services_with_a_timeout() -> None:
    def respond(command: list[str]) -> CommandResult:
        return ok(AVAHI_SAMPLE.encode()) if command[-1] == "_adb-tls-connect._tcp" else ok()

    runner = FakeRunner(respond)
    found = await AvahiSource(runner, "avahi-browse").browse()
    assert sorted(call[-1] for call in runner.calls) == ["_adb-tls-connect._tcp", "_adb-tls-pairing._tcp"]
    assert all(call[:4] == ["avahi-browse", "-r", "-t", "-p"] for call in runner.calls)
    assert {service.kind for service in found} == {CandidateKind.CONNECT}
    assert timedelta(seconds=3) == AVAHI_TIMEOUT


async def test_avahi_without_the_program_is_a_discovery_error() -> None:
    class MissingRunner:
        async def run(self, args: list[str], timeout: timedelta) -> CommandResult:
            raise CommandError(f"command not found: {args[0]}")

    with pytest.raises(DiscoveryError, match="command not found"):
        await AvahiSource(MissingRunner(), "avahi-browse").browse()


# endregion: avahi-browse

# region: fallback, cache, and matching


class FakeSource:
    def __init__(self, source: DiscoverySource, found: list[FoundService] | None, error: str = "") -> None:
        self.source = source
        self.found = found
        self.error = error
        self.calls = 0

    async def browse(self) -> list[FoundService]:
        self.calls += 1
        if self.found is None:
            raise DiscoveryError(self.error)
        return self.found


def service(kind: CandidateKind = CandidateKind.CONNECT, port: int = CONNECT_PORT) -> FoundService:
    return FoundService(name=CONNECT_NAME, kind=kind, host=HOST, port=port, model="SM-A556B")


async def test_zeroconf_first_then_avahi() -> None:
    zeroconf = FakeSource(DiscoverySource.ZEROCONF, [service()])
    avahi = FakeSource(DiscoverySource.AVAHI, [])
    found = await PhoneDiscovery([zeroconf, avahi]).discover([])
    assert (found.source, avahi.calls, found.note) == (DiscoverySource.ZEROCONF, 0, None)

    # zeroconf finds nothing: avahi-browse is asked.
    empty = FakeSource(DiscoverySource.ZEROCONF, [])
    avahi = FakeSource(DiscoverySource.AVAHI, [service()])
    found = await PhoneDiscovery([empty, avahi]).discover([])
    assert (found.source, len(found.candidates)) == (DiscoverySource.AVAHI, 1)

    # zeroconf fails: the same.
    broken = FakeSource(DiscoverySource.ZEROCONF, None, "no multicast interface")
    found = await PhoneDiscovery([broken, avahi]).discover([])
    assert found.candidates[0].source is DiscoverySource.AVAHI


async def test_the_notes_when_nothing_is_found() -> None:
    nothing = await PhoneDiscovery(
        [FakeSource(DiscoverySource.ZEROCONF, []), FakeSource(DiscoverySource.AVAHI, None, "command not found")]
    ).discover([])
    assert (nothing.candidates, nothing.note) == ([], NOTHING_FOUND)
    failed = await PhoneDiscovery(
        [FakeSource(DiscoverySource.ZEROCONF, None, "no network"), FakeSource(DiscoverySource.AVAHI, None, "no avahi")]
    ).discover([])
    assert failed.note == "zeroconf: no network; avahi-browse: no avahi"
    assert failed.source is None


async def test_the_result_is_cached_for_a_few_seconds() -> None:
    now = [100.0]
    source = FakeSource(DiscoverySource.ZEROCONF, [service()])
    discovery = PhoneDiscovery([source], clock=lambda: now[0])
    await discovery.discover([])
    now[0] += 4
    await discovery.discover([])
    assert source.calls == 1
    now[0] += 2
    await discovery.discover([])
    assert source.calls == 2


def test_a_candidate_matches_the_adb_device_with_the_same_address() -> None:
    connect, pairing = service(), service(CandidateKind.PAIRING, PAIRING_PORT)
    by_address = [AdbDevice(serial=f"{HOST}:{CONNECT_PORT}", state="device")]
    by_name = [AdbDevice(serial=f"{CONNECT_NAME}._adb-tls-connect._tcp", state="device")]
    old_port = [AdbDevice(serial=f"{HOST}:40000", state="offline")]
    assert matching_serial(connect, by_address) == f"{HOST}:{CONNECT_PORT}"
    assert matching_serial(connect, by_name) == f"{CONNECT_NAME}._adb-tls-connect._tcp"
    # The phone changed its port: the old serial is not this candidate, so the page offers Connect.
    assert matching_serial(connect, old_port) is None
    # A pairing candidate on the host of a connected phone: that phone.
    assert matching_serial(pairing, old_port) == f"{HOST}:40000"


# endregion: fallback, cache, and matching

# region: phone_devices and the page


NETWORK = [
    FoundService(name=CONNECT_NAME, kind=CandidateKind.CONNECT, host="192.0.2.60", port=41000, model="SM-A556B"),
    FoundService(name=PAIRING_NAME, kind=CandidateKind.PAIRING, host="192.0.2.60", port=37000),
]


async def test_phone_devices_lists_the_network_read_only(bench: Bench) -> None:  # noqa: F811
    bench.services.discovery = PhoneDiscovery([FakeSource(DiscoverySource.ZEROCONF, NETWORK)])
    async with Client(bench.server) as client:
        listed = await client.call_tool("phone_devices", {})
    result = listed.structured_content
    assert result is not None
    assert [(item["kind"], item["address"], item["adb_serial"]) for item in result["network"]] == [
        ("connect", "192.0.2.60:41000", None),
        ("pairing", "192.0.2.60:37000", None),
    ]
    assert result["network_note"] is None
    assert "exact address" in result["note"]
    # Listing sends no connect or pair, and no command to the other devices.
    assert not [call for call in bench.calls() if call[:1] in (["connect"], ["pair"])]
    assert TV not in bench.device_serials_used()


def test_the_page_lists_the_network_and_connects_on_the_users_click(bench: Bench) -> None:  # noqa: F811
    bench.services.discovery = PhoneDiscovery([FakeSource(DiscoverySource.ZEROCONF, NETWORK)])
    bench.monitor.device_panel = bench.panel
    # The phone was paired before; then it changed its wireless-debugging port (adb has no device for it now).
    state = json.loads(bench.state_file.read_text())
    bench.state_file.write_text(json.dumps({**state, "paired_devices": [PHONE]}))
    client = TestClient(create_app(bench.monitor), base_url="http://127.0.0.1:18766")
    listed = client.get("/api/devices").json()
    assert [item["address"] for item in listed["network"]] == ["192.0.2.60:41000", "192.0.2.60:37000"]
    assert not [call for call in bench.calls() if call[:1] == ["connect"]]
    # The user presses Connect: adb connect to that address; after it, the phone is in adb with that address.
    page = {"Origin": "http://127.0.0.1:18766"}
    connected = client.post("/api/devices/connect", json={"address": "192.0.2.60:41000"}, headers=page).json()
    assert connected["steps"][0]["ok"], connected["steps"]
    assert ["connect", "192.0.2.60:41000"] in bench.calls()
    assert connected["devices"]["network"][0]["adb_serial"] == "192.0.2.60:41000"


def test_the_page_has_the_network_list(tmp_path: Path) -> None:
    page = (Path(__file__).resolve().parents[1] / "debug_devices_mcp" / "ui" / "static" / "index.html").read_text()
    assert 'id="devices-network-table"' in page


# endregion: phone_devices and the page
