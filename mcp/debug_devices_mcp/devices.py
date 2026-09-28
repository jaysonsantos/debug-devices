"""The phones that adb sees, and the phone that the user selected (in the monitor page, or in the config).

Safety (AGENTS.md): only the user selects the phone. The server never picks a device by itself (also not the only
one), and it sends device commands only with `-s <selected serial>`. Our camera app check (`pm path`) runs only on
the selected phone: any other device can be another device (a Fire TV on the network, or a TV on USB), so it gets
no command until the user selects it.
"""

import asyncio
import logging
import re
from enum import StrEnum

from pydantic import BaseModel

from debug_devices_mcp.adb import Adb, AdbDevice, AdbError, MdnsService
from debug_devices_mcp.constants import adb
from debug_devices_mcp.discovery import NetworkCandidate, NetworkDiscovery, PhoneDiscovery
from debug_devices_mcp.ui.settings import SettingsStore

logger = logging.getLogger(__name__)

# A network serial has a port: `192.0.2.23:5555`, `firetv.lan:5555`, `[fe80::1]:5555`. A USB serial never has a
# colon.
PORT_SEPARATOR = ":"
# A device that adb connected by itself over mDNS: `adb-<id>._adb-tls-connect._tcp` or `adb-<id>._adb._tcp`.
MDNS_SERIAL = re.compile(r"\._adb(?:-tls-connect)?\._tcp\.?$")
MODEL_KEY = "model"
PRODUCT_KEY = "product"
STATE_UNAUTHORIZED = "unauthorized"
APP_MISSING = "app not installed"
APP_NOT_SELECTED = "app: unknown (not selected; no command goes to a device that is not selected)"
MDNS_UNSUPPORTED = "not supported by this adb (use Pair or Connect with the address from the phone)"
SELECTED_GONE = (
    "the selected phone {serial} is gone: it is not in adb devices. The user connects it again in the monitor page "
    "(Devices: Found on the network, or Pair), or presses Disconnect there"
)


class Transport(StrEnum):
    USB = "usb"
    WIFI = "wifi"


class SelectionSource(StrEnum):
    # The user chose it in the monitor page (ui-settings.json `adb_serial`).
    PAGE = "page"
    # --adb-serial or DEBUG_DEVICES_ADB_SERIAL.
    CONFIG = "config"
    NONE = "none"


def transport_of(serial: str) -> Transport:
    """Wi-Fi for any network serial (IPv4, IPv6, or a host name with a port, or an mDNS name); else USB."""
    return Transport.WIFI if PORT_SEPARATOR in serial or MDNS_SERIAL.search(serial) else Transport.USB


class DeviceInfo(BaseModel):
    serial: str
    transport: Transport
    # device, unauthorized, offline, ...
    state: str
    model: str
    product: str
    # Our camera app: True, False, or None (not checked, or the device did not answer).
    app_installed: bool | None
    selected: bool
    note: str | None = None


class DeviceList(BaseModel):
    devices: list[DeviceInfo]
    selected_serial: str | None
    selected_from: SelectionSource
    # Wireless-debugging phones on the network that adb can connect (when this adb supports mDNS).
    mdns: list[MdnsService]
    mdns_note: str | None
    note: str
    # Wireless-debugging phones that the server found itself (zeroconf or avahi-browse): the address for Connect
    # (kind connect) or Pair (kind pairing), and the adb serial when the phone is already in `adb devices`.
    network: list[NetworkCandidate] = []
    network_note: str | None = None
    # The selected serial is not in `adb devices` (the phone left the Wi-Fi, or changed its port). The selection
    # stays until the user changes it, but the phone is not connected.
    selected_gone: bool = False
    selected_note: str | None = None


LIST_NOTE = (
    "Read-only. Only the user selects the phone, in the monitor page (Devices). The agent cannot select, pair, or "
    "connect a phone. `network` lists the wireless-debugging phones found on the network: tell the user the exact "
    "address, and the user connects (kind connect) or pairs (kind pairing, with the code on the phone) in the page."
)


class SelectionNotSavedError(Exception):
    """The settings file could not be written: the selection did not change."""


class PhoneSelection:
    """The selected serial, stored in the settings file (shared by all MCP servers of the user)."""

    def __init__(self, store: SettingsStore | None = None) -> None:
        self._store = store
        self._value: str | None = None

    @property
    def page_serial(self) -> str | None:
        if self._store is None:
            return self._value
        return self._store.current().adb_serial

    def set(self, serial: str | None) -> None:
        """Save the choice. Raises SelectionNotSavedError when the file cannot be written: the choice is then not in
        effect (the file is the source of truth for every server), and the caller must say so. For code without an
        event loop; on the event loop, `save`."""
        if self._store is not None:
            try:
                self._store.update(lambda saved: saved.model_copy(update={"adb_serial": serial}))
            except OSError as exc:
                raise SelectionNotSavedError(f"cannot save the selected phone: {exc}") from exc
        self._value = serial

    async def save(self, serial: str | None) -> None:
        """`set` for code on the event loop: the file lock wait runs in a worker thread (N3 of QA round 6)."""
        if self._store is not None:
            try:
                await self._store.update_async(lambda saved: saved.model_copy(update={"adb_serial": serial}))
            except OSError as exc:
                raise SelectionNotSavedError(f"cannot save the selected phone: {exc}") from exc
        self._value = serial

    def effective(self, config_serial: str) -> tuple[str, SelectionSource]:
        """The serial for the phone tools: the page choice over the config. Empty: no choice, and the phone tools
        refuse (the server never picks a device by itself)."""
        if self.page_serial:
            return self.page_serial, SelectionSource.PAGE
        if config_serial:
            return config_serial, SelectionSource.CONFIG
        return "", SelectionSource.NONE


async def app_state(bridge: Adb, device: AdbDevice, selected: str) -> tuple[bool | None, str | None]:
    """`pm path` only on the selected device (S2 of QA round 4); the others show "unknown (not selected)"."""
    if device.state != adb.STATE_DEVICE:
        return (
            None,
            f"state {device.state}: allow USB debugging on the phone" if device.state == STATE_UNAUTHORIZED else None,
        )
    if device.serial != selected:
        return None, APP_NOT_SELECTED
    installed = await bridge.app_installed(device.serial)
    return installed, APP_MISSING if installed is False else None


def mdns_note(error: str) -> str:
    """A short note for the page and the agent: this adb cannot discover wireless-debugging phones."""
    if adb.MDNS_UNSUPPORTED_MARKER in error:
        return MDNS_UNSUPPORTED
    return error


async def list_devices(
    bridge: Adb, selection: PhoneSelection, config_serial: str, discovery: PhoneDiscovery | None = None
) -> DeviceList:
    """The adb devices with their state, and (with `discovery`) the wireless-debugging phones on the network."""
    selected, source = selection.effective(config_serial)
    try:
        devices = await bridge.devices()
    except AdbError as exc:
        found = await discovery.discover([]) if discovery is not None else None
        return DeviceList(
            devices=[],
            selected_serial=selected or None,
            selected_from=source,
            mdns=[],
            mdns_note=None,
            note=str(exc),
            **network_fields(found),
        )
    gone = bool(selected) and all(device.serial != selected for device in devices)
    # The network browse (about 1.5 s) runs while adb checks the app on the devices.
    network = asyncio.create_task(discovery.discover(devices)) if discovery is not None else None
    infos = []
    for device in devices:
        installed, note = await app_state(bridge, device, selected)
        infos.append(
            DeviceInfo(
                serial=device.serial,
                transport=transport_of(device.serial),
                state=device.state,
                model=device.detail(MODEL_KEY),
                product=device.detail(PRODUCT_KEY),
                app_installed=installed,
                selected=device.serial == selected,
                note=note,
            )
        )
    mdns = await bridge.mdns_services()
    return DeviceList(
        devices=infos,
        selected_serial=selected or None,
        selected_from=source,
        mdns=mdns if isinstance(mdns, list) else [],
        mdns_note=mdns_note(mdns) if isinstance(mdns, str) else None,
        note=LIST_NOTE,
        selected_gone=gone,
        selected_note=SELECTED_GONE.format(serial=selected) if gone else None,
        **network_fields(await network if network is not None else None),
    )


def network_fields(found: NetworkDiscovery | None) -> dict:
    if found is None:
        return {}
    return {"network": found.candidates, "network_note": found.note}
