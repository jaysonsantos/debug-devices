"""The phones that adb sees, and the phone that the user selected (in the monitor page, or in the config).

Safety (AGENTS.md): only the user selects the phone. The server never picks one of several devices by itself, and
it sends device commands only with `-s <serial>`. Our camera app check (`pm path`) runs only on USB devices (the
user plugged them in) and on the selected phone: a Wi-Fi serial can be another device on the network (a Fire TV),
so it gets no command until the user selects it.
"""

import logging
import re
from enum import StrEnum

from pydantic import BaseModel

from debug_devices_mcp.adb import Adb, AdbDevice, AdbError, MdnsService
from debug_devices_mcp.constants import adb
from debug_devices_mcp.ui.settings import SettingsStore

logger = logging.getLogger(__name__)

WIFI_SERIAL = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}:\d+$")
# A phone that adb connected by itself over wireless debugging (mDNS).
MDNS_SERIAL_MARKER = "._adb-tls-connect._tcp"
MODEL_KEY = "model"
PRODUCT_KEY = "product"
STATE_UNAUTHORIZED = "unauthorized"
APP_MISSING = "app not installed"
MDNS_UNSUPPORTED = "not supported by this adb (use Pair or Connect with the address from the phone)"
WIFI_APP_UNKNOWN = "select it to check the app (no command goes to an unselected Wi-Fi device)"


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
    return Transport.WIFI if WIFI_SERIAL.match(serial) or MDNS_SERIAL_MARKER in serial else Transport.USB


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


LIST_NOTE = (
    "Read-only. Only the user selects the phone, in the monitor page (Devices). The agent cannot select, pair, or "
    "connect a phone."
)


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
        self._value = serial
        if self._store is None:
            return
        try:
            saved = self._store.load()
            self._store.save(saved.model_copy(update={"adb_serial": serial}))
        except OSError as exc:
            logger.warning("cannot save the selected phone: %s", exc)

    def effective(self, config_serial: str) -> tuple[str, SelectionSource]:
        """The serial for the phone tools: the page choice over the config. Empty: no choice (only one device
        is then used; several devices stay an error)."""
        if self.page_serial:
            return self.page_serial, SelectionSource.PAGE
        if config_serial:
            return config_serial, SelectionSource.CONFIG
        return "", SelectionSource.NONE


async def app_state(bridge: Adb, device: AdbDevice, selected: str) -> tuple[bool | None, str | None]:
    if device.state != adb.STATE_DEVICE:
        return (
            None,
            f"state {device.state}: allow USB debugging on the phone" if device.state == STATE_UNAUTHORIZED else None,
        )
    if transport_of(device.serial) is Transport.WIFI and device.serial != selected:
        return None, WIFI_APP_UNKNOWN
    installed = await bridge.app_installed(device.serial)
    return installed, APP_MISSING if installed is False else None


def mdns_note(error: str) -> str:
    """A short note for the page and the agent: this adb cannot discover wireless-debugging phones."""
    if adb.MDNS_UNSUPPORTED_MARKER in error:
        return MDNS_UNSUPPORTED
    return error


async def list_devices(bridge: Adb, selection: PhoneSelection, config_serial: str) -> DeviceList:
    selected, source = selection.effective(config_serial)
    try:
        devices = await bridge.devices()
    except AdbError as exc:
        return DeviceList(
            devices=[], selected_serial=selected or None, selected_from=source, mdns=[], mdns_note=None, note=str(exc)
        )
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
    )
