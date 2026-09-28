#!/usr/bin/env python3
"""Fake adb for MCP tests with scripts/fake_phone.py. It never calls the real adb.

By default it shows one device and accepts `forward` and `shell am start`. With FAKE_ADB_STATE (a JSON file), it has
several devices (USB and Wi-Fi serials, any state) and also `tcpip`, `connect`, `disconnect`, `pair`,
`shell ip -f inet addr show wlan0`, and `shell pm path <package>`; `connect` and `pair` change the file:

    {"devices": [{"serial": "R5CT1234567", "state": "device", "model": "SM_A556B", "product": "a55",
                  "app": true, "wlan": "192.0.2.23"}],
     "pairing": {"192.0.2.23:37000": "123456"}, "wireless": {"192.0.2.23:41000": "R5CT1234567"}}

`wireless` maps a "connect" address of wireless debugging to the device that it reaches (after `pair`).
Start the fake phone on the local forward port of the MCP, then point the MCP at this file:

    python3 scripts/fake_phone.py --port 18765 &
    DEBUG_DEVICES_ADB_PATH=scripts/fake_adb.py uv run debug-devices-mcp

Set FAKE_ADB_LOG to a file to record every call (one line per call).
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

# region: constants

FAKE_SERIAL = "fake-phone-0001"
DEVICE_LINE = f"{FAKE_SERIAL}\tdevice product:fake model:FakePhone device:fake transport_id:1"
DEVICES_HEADER = "List of devices attached"
EXIT_OK = 0
EXIT_ERROR = 1


class Env(StrEnum):
    LOG = "FAKE_ADB_LOG"
    SERIAL = "FAKE_ADB_SERIAL"
    STATE = "FAKE_ADB_STATE"


class Command(StrEnum):
    DEVICES = "devices"
    FORWARD = "forward"
    SHELL = "shell"
    VERSION = "version"
    TCPIP = "tcpip"
    CONNECT = "connect"
    DISCONNECT = "disconnect"
    PAIR = "pair"
    MDNS = "mdns"


STATE_DEVICE = "device"
TCPIP_PORT_KEY = "tcpip"
PM_PATH = ("pm", "path")
IP_ADDR = ("ip", "-f", "inet", "addr", "show", "wlan0")
APP_APK = "/data/app/~~fake==/dev.jayson.debugdevices.camera-1/base.apk"


SERIAL_FLAG = "-s"
LONG_FLAG = "-l"
AM_START = ("am", "start")
TCP_PREFIX = "tcp:"
REMOVE_FLAG = "--remove"
FORWARD_SPEC_COUNT = 2  # tcp:<local> tcp:<remote>

# endregion: constants


def log_call(argv: list[str]) -> None:
    log_file = os.environ.get(Env.LOG)
    if log_file:
        with Path(log_file).open("a") as handle:
            handle.write(f"{datetime.now(UTC).isoformat()} {shlex.join(argv)}\n")


def fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return EXIT_ERROR


# region: several devices (FAKE_ADB_STATE)


def load_state(path: Path) -> dict:
    return json.loads(path.read_text())


def save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, indent=2))


def device_line(device: dict) -> str:
    extra = ""
    if device["state"] == STATE_DEVICE:
        extra = f" product:{device.get('product', 'fake')} model:{device.get('model', 'Fake')} device:fake"
    return f"{device['serial']}\t{device['state']}{extra} transport_id:1"


def add_wifi_device(state: dict, serial: str, source: dict) -> None:
    if any(device["serial"] == serial for device in state["devices"]):
        return
    state["devices"].append({**source, "serial": serial, "state": STATE_DEVICE, TCPIP_PORT_KEY: None})


def connect(state: dict, address: str) -> str:
    """adb connect prints a line and exits 0 also when it fails (like the real adb)."""
    if any(device["serial"] == address for device in state["devices"]):
        return f"already connected to {address}"
    host, _, port = address.rpartition(":")
    for device in state["devices"]:
        if device.get("wlan") == host and str(device.get(TCPIP_PORT_KEY)) == port:
            add_wifi_device(state, address, device)
            return f"connected to {address}"
    target = state.get("wireless", {}).get(address)
    if target is not None and target in state.get("paired_devices", []):
        source = next(device for device in state["devices"] if device["serial"] == target)
        add_wifi_device(state, address, source)
        return f"connected to {address}"
    return f"failed to connect to '{address}': Connection refused"


type Handler = Callable[[Path, dict, dict | None, list[str]], int]


def do_devices(path: Path, state: dict, device: dict | None, rest: list[str]) -> int:
    print(DEVICES_HEADER)
    for item in state["devices"]:
        print(device_line(item) if LONG_FLAG in rest else f"{item['serial']}\t{item['state']}")
    print()
    return EXIT_OK


def do_mdns(path: Path, state: dict, device: dict | None, rest: list[str]) -> int:
    return fail("mdns is not supported by this version of adb.")


def do_connect(path: Path, state: dict, device: dict | None, rest: list[str]) -> int:
    print(connect(state, rest[0]))
    save_state(path, state)
    return EXIT_OK


def do_disconnect(path: Path, state: dict, device: dict | None, rest: list[str]) -> int:
    state["devices"] = [item for item in state["devices"] if item["serial"] != rest[0]]
    save_state(path, state)
    print(f"disconnected {rest[0]}")
    return EXIT_OK


def do_pair(path: Path, state: dict, device: dict | None, rest: list[str]) -> int:
    address, code = rest[0], rest[1]
    if state.get("pairing", {}).get(address) != code:
        # The real adb exits 1 on a wrong code.
        return fail("Failed: Unable to start pairing client.")
    state.setdefault("paired_devices", []).extend(state.get("wireless", {}).values())
    save_state(path, state)
    print(f"Successfully paired to {address} [guid=adb-fake]")
    return EXIT_OK


def do_tcpip(path: Path, state: dict, device: dict | None, rest: list[str]) -> int:
    assert device is not None
    device[TCPIP_PORT_KEY] = int(rest[0])
    save_state(path, state)
    print(f"restarting in TCP mode port: {rest[0]}")
    return EXIT_OK


def do_forward(path: Path, state: dict, device: dict | None, rest: list[str]) -> int:
    specs = [spec for spec in rest if spec != REMOVE_FLAG]
    if not all(spec.startswith(TCP_PREFIX) for spec in specs):
        return fail(f"forward needs tcp: specs, got {specs}")
    print(specs[0].removeprefix(TCP_PREFIX))
    return EXIT_OK


def do_shell(path: Path, state: dict, device: dict | None, rest: list[str]) -> int:
    assert device is not None
    if tuple(rest[:2]) == AM_START:
        print(f"Starting: Intent {{ cmp={rest[-1]} }}")
    elif tuple(rest[:2]) == PM_PATH:
        if not device.get("app"):
            return EXIT_ERROR  # pm path prints nothing and exits 1 for a missing package
        print(f"package:{APP_APK}")
    elif tuple(rest) == IP_ADDR and device.get("wlan"):
        print("47: wlan0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 state UP group default qlen 3000")
        print(f"    inet {device['wlan']}/24 brd 192.0.2.255 scope global wlan0")
    elif tuple(rest) != IP_ADDR:
        return fail(f"fake adb does not support: shell {shlex.join(rest)}")
    return EXIT_OK


# Commands for the adb server itself: no -s. The others need -s <serial> (like adb with several devices).
HOST_COMMANDS: dict[str, Handler] = {
    Command.DEVICES: do_devices,
    Command.MDNS: do_mdns,
    Command.CONNECT: do_connect,
    Command.DISCONNECT: do_disconnect,
    Command.PAIR: do_pair,
}
DEVICE_COMMANDS: dict[str, Handler] = {
    Command.TCPIP: do_tcpip,
    Command.FORWARD: do_forward,
    Command.SHELL: do_shell,
}


def run_state(path: Path, args: list[str]) -> int:
    state = load_state(path)
    serial = None
    if args[:1] == [SERIAL_FLAG] and len(args) > 1:
        serial, args = args[1], args[2:]
    if not args:
        return fail("no command")
    command, rest = args[0], args[1:]
    if command in HOST_COMMANDS:
        return fail(f"{command} does not take -s") if serial else HOST_COMMANDS[command](path, state, None, rest)
    handler = DEVICE_COMMANDS.get(command)
    device = next((item for item in state["devices"] if item["serial"] == serial), None)
    if handler is None:
        return fail(f"fake adb does not support: {shlex.join(args)}")
    if device is None:
        return fail(f"device '{serial}' not found" if serial else "more than one device/emulator")
    if device["state"] != STATE_DEVICE:
        return fail(f"device {device['state']}")
    return handler(path, state, device, rest)


# endregion: several devices


def run(argv: list[str]) -> int:
    state_path = os.environ.get(Env.STATE)
    return run_state(Path(state_path), list(argv)) if state_path else run_one(argv)


def run_one(argv: list[str]) -> int:
    """The default: one device (FAKE_ADB_SERIAL)."""
    serial = os.environ.get(Env.SERIAL, FAKE_SERIAL)
    args = list(argv)
    if args[:1] == [SERIAL_FLAG]:
        if len(args) == 1:
            return fail("-s needs a serial")
        if args[1] != serial:
            return fail(f"device '{args[1]}' not found")
        args = args[2:]
    if not args:
        return fail("no command")

    match args[0]:
        case Command.VERSION:
            print("Android Debug Bridge version 1.0.41 (fake)")
        case Command.DEVICES:
            print(DEVICES_HEADER)
            print(DEVICE_LINE.replace(FAKE_SERIAL, serial) if LONG_FLAG in args else f"{serial}\tdevice")
            print()
        case Command.FORWARD:
            specs = args[1:]
            if len(specs) != FORWARD_SPEC_COUNT or not all(spec.startswith(TCP_PREFIX) for spec in specs):
                return fail(f"forward needs tcp:<local> tcp:<remote>, got {specs}")
            print(specs[0].removeprefix(TCP_PREFIX))
        case Command.SHELL if tuple(args[1:3]) == AM_START:
            component = args[-1]
            print(f"Starting: Intent {{ cmp={component} }}")
        case _:
            return fail(f"fake adb does not support: {shlex.join(args)}")
    return EXIT_OK


def main() -> int:
    log_call(sys.argv[1:])
    return run(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
