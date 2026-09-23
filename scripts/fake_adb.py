#!/usr/bin/env python3
"""Fake adb for MCP tests with scripts/fake_phone.py. It never calls the real adb.

It shows one device, accepts `forward` and `shell am start`, and does nothing else.
Start the fake phone on the local forward port of the MCP, then point the MCP at this file:

    python3 scripts/fake_phone.py --port 18765 &
    DEBUG_DEVICES_ADB_PATH=scripts/fake_adb.py uv run debug-devices-mcp

Set FAKE_ADB_LOG to a file to record every call (one line per call).
"""

from __future__ import annotations

import os
import shlex
import sys
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


class Command(StrEnum):
    DEVICES = "devices"
    FORWARD = "forward"
    SHELL = "shell"
    VERSION = "version"


SERIAL_FLAG = "-s"
LONG_FLAG = "-l"
AM_START = ("am", "start")
TCP_PREFIX = "tcp:"
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


def run(argv: list[str]) -> int:
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
