"""Find wireless-debugging phones on the network without the mDNS of adb.

Some adb builds have no mDNS ("mdns not supported by this adb"), and a phone can change its wireless-debugging port
(Samsung does). The server browses the two adb services itself: `_adb-tls-connect._tcp` (a paired phone that takes
`adb connect`) and `_adb-tls-pairing._tcp` (a phone that shows a pairing code). First with python-zeroconf; when it
finds nothing or fails, with `avahi-browse`. The result is cached for a few seconds.

Read-only: nothing connects or pairs by itself. The user chooses in the monitor page (Devices), and the agent only
sees the list (phone_devices). Plain `_adb._tcp` services (for example a Fire TV in adb TCP mode) are not listed.
"""

import asyncio
import logging
import re
import time
from collections.abc import Callable, Sequence
from datetime import timedelta
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, computed_field
from zeroconf import Error as ZeroconfError
from zeroconf import IPVersion, ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

from debug_devices_mcp.adb import AdbDevice
from debug_devices_mcp.process import CommandError, CommandRunner

logger = logging.getLogger(__name__)

# region: constants

# Listen this long for answers, then resolve each service in at most RESOLVE_TIME.
BROWSE_TIME = timedelta(seconds=1.5)
RESOLVE_TIME = timedelta(seconds=1)
# avahi-browse -t ends after it dumped its cache; this is the limit for one service type.
AVAHI_TIMEOUT = timedelta(seconds=3)
# The page and phone_devices ask again within this time: the same result.
CACHE_TIME = timedelta(seconds=5)
MDNS_DOMAIN = "local"
# avahi-browse: resolve, terminate after the cache dump, parsable output.
AVAHI_FLAGS = ("-r", "-t", "-p")
AVAHI_RESOLVED = "="
AVAHI_FIELD_SEPARATOR = ";"
# `=;interface;protocol;name;type;domain;host;address;port;txt`
AVAHI_RESOLVED_FIELDS = 9
# avahi-browse writes special characters of a name as a backslash and three decimal digits (`\032` is a space).
AVAHI_ESCAPE = re.compile(r"\\(\d{3})")
IPV4 = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
# The TXT record of the adb services has the phone model (`name=SM-S901B`).
MODEL_KEY = "name"
TXT_ITEM = re.compile(r'"([^"]*)"')
NOTHING_FOUND = (
    "no wireless-debugging phone found on the network (Developer options, Wireless debugging must be on; the "
    "phone and this PC must be on the same network)"
)

# endregion: constants


class CandidateKind(StrEnum):
    # A paired phone: `adb connect` to this address.
    CONNECT = "connect"
    # The phone shows "Pair device with pairing code": `adb pair` to this address with the code.
    PAIRING = "pairing"


SERVICE_TYPES = {CandidateKind.CONNECT: "_adb-tls-connect._tcp", CandidateKind.PAIRING: "_adb-tls-pairing._tcp"}


class DiscoverySource(StrEnum):
    ZEROCONF = "zeroconf"
    AVAHI = "avahi-browse"


class DiscoveryError(Exception):
    """A discovery source did not work (no network, no avahi-browse, ...)."""


class FoundService(BaseModel):
    # The service instance name, for example adb-R5CT123ABC-a1B2c3.
    name: str
    kind: CandidateKind
    host: str
    port: int
    # From the TXT record, when the phone sends it (for example SM-S901B).
    model: str = ""

    @computed_field
    @property
    def address(self) -> str:
        """For `adb connect` (connect kind) or `adb pair` (pairing kind)."""
        return f"{self.host}:{self.port}"


class NetworkCandidate(FoundService):
    source: DiscoverySource
    # The adb device with this address or this mDNS name (connect kind), or on this host (pairing kind). None: the
    # phone is not in `adb devices`.
    adb_serial: str | None = None


class NetworkDiscovery(BaseModel):
    candidates: list[NetworkCandidate]
    source: DiscoverySource | None
    note: str | None


# region: sources


class ServiceSource(Protocol):
    source: DiscoverySource

    async def browse(self) -> list[FoundService]: ...


def instance_name(full_name: str, service_type: str) -> str:
    """`adb-X-y._adb-tls-connect._tcp.local.` -> `adb-X-y`."""
    suffix = f".{service_type}.{MDNS_DOMAIN}."
    return full_name.removesuffix(suffix)


def model_of(properties: dict[bytes, bytes | None]) -> str:
    value = properties.get(MODEL_KEY.encode())
    return value.decode(errors="replace") if value else ""


class ZeroconfSource:
    """python-zeroconf: browse both service types for BROWSE_TIME, then resolve the IPv4 addresses. The factories are
    for the tests (a fake service browser)."""

    source = DiscoverySource.ZEROCONF

    def __init__(
        self,
        zeroconf_factory: Callable[..., Any] = AsyncZeroconf,
        browser_factory: Callable[..., Any] = AsyncServiceBrowser,
        info_factory: Callable[[str, str], Any] = AsyncServiceInfo,
        browse_time: timedelta = BROWSE_TIME,
    ) -> None:
        self._zeroconf_factory = zeroconf_factory
        self._browser_factory = browser_factory
        self._info_factory = info_factory
        self._browse_time = browse_time

    async def browse(self) -> list[FoundService]:
        kinds = {f"{service_type}.{MDNS_DOMAIN}.": kind for kind, service_type in SERVICE_TYPES.items()}
        seen: set[tuple[str, str]] = set()

        def on_change(zeroconf: Any, service_type: str, name: str, state_change: ServiceStateChange) -> None:
            if state_change is not ServiceStateChange.Removed:
                seen.add((service_type, name))

        try:
            zeroconf = self._zeroconf_factory(ip_version=IPVersion.V4Only)
        except (OSError, ZeroconfError) as exc:
            raise DiscoveryError(f"zeroconf cannot start: {exc}") from exc
        browser = None
        try:
            browser = self._browser_factory(zeroconf.zeroconf, list(kinds), handlers=[on_change])
            await asyncio.sleep(self._browse_time.total_seconds())
            found = []
            for service_type, name in sorted(seen):
                info = self._info_factory(service_type, name)
                if not await info.async_request(zeroconf.zeroconf, RESOLVE_TIME.total_seconds() * 1000):
                    continue
                for host in info.parsed_addresses(IPVersion.V4Only):
                    found.append(
                        FoundService(
                            name=instance_name(name, service_type.removesuffix(f".{MDNS_DOMAIN}.")),
                            kind=kinds[service_type],
                            host=host,
                            port=info.port,
                            model=model_of(info.properties),
                        )
                    )
            return found
        except (OSError, ZeroconfError) as exc:
            raise DiscoveryError(f"zeroconf failed: {exc}") from exc
        finally:
            if browser is not None:
                await browser.async_cancel()
            await zeroconf.async_close()


def unescape_avahi(text: str) -> str:
    return AVAHI_ESCAPE.sub(lambda match: chr(int(match.group(1))), text)


def parse_avahi(output: str, kind: CandidateKind) -> list[FoundService]:
    """`avahi-browse -rtp`: the resolved (`=`) lines with an IPv4 address. The protocol column can say IPv6 for an
    IPv4 address (seen on the bench PC): the address decides. One entry per name and address."""
    found: dict[tuple[str, str, int], FoundService] = {}
    for line in output.splitlines():
        fields = line.split(AVAHI_FIELD_SEPARATOR)
        if len(fields) < AVAHI_RESOLVED_FIELDS or fields[0] != AVAHI_RESOLVED:
            continue
        _, _, _, name, _, _, _, host, port, *txt = fields
        if not IPV4.match(host) or not port.isdigit():
            continue
        model = ""
        for item in TXT_ITEM.findall(AVAHI_FIELD_SEPARATOR.join(txt)):
            key, _, value = item.partition("=")
            if key == MODEL_KEY:
                model = value
        service = FoundService(name=unescape_avahi(name), kind=kind, host=host, port=int(port), model=model)
        found.setdefault((service.name, host, service.port), service)
    return list(found.values())


class AvahiSource:
    """`avahi-browse -rtp <service>` for both service types (it needs the avahi daemon of the system)."""

    source = DiscoverySource.AVAHI

    def __init__(self, runner: CommandRunner, path: str, timeout: timedelta = AVAHI_TIMEOUT) -> None:
        self._runner = runner
        self._path = path
        self._timeout = timeout

    async def _browse_one(self, kind: CandidateKind) -> list[FoundService]:
        try:
            result = await self._runner.run([self._path, *AVAHI_FLAGS, SERVICE_TYPES[kind]], self._timeout)
        except CommandError as exc:
            raise DiscoveryError(str(exc)) from exc
        if not result.ok:
            stderr = result.stderr.decode(errors="replace").strip()
            raise DiscoveryError(f"{self._path} exited with {result.returncode}: {stderr}")
        return parse_avahi(result.stdout.decode(errors="replace"), kind)

    async def browse(self) -> list[FoundService]:
        results = await asyncio.gather(*(self._browse_one(kind) for kind in CandidateKind))
        return [service for found in results for service in found]


# endregion: sources


def matching_serial(service: FoundService, devices: Sequence[AdbDevice]) -> str | None:
    """The adb device of this service: the same address or mDNS name (connect), or the same host (pairing)."""
    for device in devices:
        if service.kind is CandidateKind.CONNECT:
            if device.serial == service.address or device.serial.startswith(f"{service.name}."):
                return device.serial
        elif device.serial.partition(":")[0] == service.host:
            return device.serial
    return None


class PhoneDiscovery:
    """The sources in order (zeroconf, then avahi-browse), with a short cache."""

    def __init__(self, sources: Sequence[ServiceSource], clock: Callable[[], float] = time.monotonic) -> None:
        self._sources = list(sources)
        self._clock = clock
        self._lock = asyncio.Lock()
        self._cached: tuple[float, list[FoundService], DiscoverySource | None, str | None] | None = None

    @classmethod
    def default(cls, runner: CommandRunner, avahi_browse_path: str) -> PhoneDiscovery:
        return cls([ZeroconfSource(), AvahiSource(runner, avahi_browse_path)])

    async def _found(self) -> tuple[list[FoundService], DiscoverySource | None, str | None]:
        async with self._lock:
            now = self._clock()
            if self._cached is not None and now - self._cached[0] < CACHE_TIME.total_seconds():
                return self._cached[1:]
            found: list[FoundService] = []
            source: DiscoverySource | None = None
            problems = []
            for candidate in self._sources:
                try:
                    found = await candidate.browse()
                except DiscoveryError as exc:
                    logger.info("phone discovery with %s failed: %s", candidate.source, exc)
                    problems.append(f"{candidate.source}: {exc}")
                    continue
                source = candidate.source
                if found:
                    break
            if found:
                note = None
            elif source is not None:
                note = NOTHING_FOUND
            else:
                note = "; ".join(problems)
            self._cached = (now, found, source, note)
            return found, source, note

    async def discover(self, devices: Sequence[AdbDevice]) -> NetworkDiscovery:
        found, source, note = await self._found()
        candidates = [
            NetworkCandidate(
                **service.model_dump(exclude={"address"}), source=source, adb_serial=matching_serial(service, devices)
            )
            for service in found
            if source is not None
        ]
        return NetworkDiscovery(candidates=candidates, source=source, note=note)
