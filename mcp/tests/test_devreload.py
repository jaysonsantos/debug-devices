"""The development reload proxy, with a small fake server as the child. No real server and no hardware."""

import asyncio
import json
import os
import sys
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from debug_devices_mcp.devreload import (
    METHOD_TOOLS_LIST_CHANGED,
    RELOADED_MESSAGE,
    REPLAY_ID_PREFIX,
    DevReloadProxy,
    ReloadOptions,
    changed_files,
    snapshot,
)

FAKE_SERVER = """
import json, os, pathlib, sys, time
state = pathlib.Path(sys.argv[1])
if (state / "broken").exists():
    print("SyntaxError: broken on purpose", file=sys.stderr)
    sys.exit(3)
if (state / "slow_start").exists():
    time.sleep(float((state / "slow_start").read_text()))
version = (state / "version").read_text().strip()
for line in sys.stdin:
    message = json.loads(line)
    method, message_id = message.get("method"), message.get("id")
    with (state / "log").open("a") as log:
        log.write(json.dumps({"pid": os.getpid(), "method": method, "id": message_id}) + "\\n")
    if message_id is None:
        continue
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake"}}
    elif method == "tools/call":
        if message["params"]["name"] == "slow":
            time.sleep(60)
        result = {"content": [{"type": "text", "text": version}]}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": message_id, "result": result}), flush=True)
"""

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}},
}
INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}
TIMEOUT_SECONDS = 15
POLL_SECONDS = 0.01


async def wait_until(condition: Callable[[], bool]) -> None:
    for _ in range(round(TIMEOUT_SECONDS / POLL_SECONDS)):
        if condition():
            return
        await asyncio.sleep(POLL_SECONDS)
    raise AssertionError("the condition did not become true in time")


def call(message_id: int, tool: str = "version") -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "method": "tools/call", "params": {"name": tool, "arguments": {}}}


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.state = tmp_path / "state"
        self.state.mkdir()
        (self.state / "version").write_text("v1")
        self.src = tmp_path / "src"
        self.src.mkdir()
        self.module = self.src / "module.py"
        self.module.write_text("VALUE = 1\n")
        server = tmp_path / "fake_server.py"
        server.write_text(FAKE_SERVER)
        options = ReloadOptions(
            command=[sys.executable, str(server), str(self.state)],
            watch_dir=self.src,
            poll_interval=timedelta(milliseconds=50),
            debounce=timedelta(milliseconds=50),
            stop_timeout=timedelta(milliseconds=500),
            start_timeout=timedelta(seconds=5),
        )
        self.reader = asyncio.StreamReader()
        self.received: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        async def write(data: bytes) -> None:
            self.received.put_nowait(json.loads(data))

        self.proxy = DevReloadProxy(options, self.reader, write)
        self.task = asyncio.create_task(self.proxy.run())

    def send(self, message: dict[str, Any]) -> None:
        self.reader.feed_data(json.dumps(message).encode() + b"\n")

    async def receive(self) -> dict[str, Any]:
        return await asyncio.wait_for(self.received.get(), TIMEOUT_SECONDS)

    async def response(self, message_id: object) -> dict[str, Any]:
        while True:
            message = await self.receive()
            if message.get("id") == message_id and "method" not in message:
                return message

    async def list_changed(self) -> None:
        while (await self.receive()).get("method") != METHOD_TOOLS_LIST_CHANGED:
            pass

    async def start(self) -> dict[str, Any]:
        self.send(INITIALIZE)
        answer = await self.response(1)
        self.send(INITIALIZED)
        return answer

    def touch(self) -> None:
        stat = self.module.stat()
        os.utime(self.module, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

    def log(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in (self.state / "log").read_text().splitlines()]

    async def close(self) -> None:
        self.reader.feed_eof()
        await asyncio.wait_for(self.task, TIMEOUT_SECONDS)


@pytest.fixture
async def harness(tmp_path: Path) -> Harness:
    proxy = Harness(tmp_path)
    yield proxy
    if not proxy.task.done():
        await proxy.close()


def test_snapshot_watches_code_and_static_files(tmp_path: Path) -> None:
    for name in ("a.py", "b.html", "c.js", "d.css", "e.txt"):
        (tmp_path / name).write_text("x")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "a.cpython-314.pyc.py").write_text("x")
    before = snapshot(tmp_path)
    assert {path.name for path in before} == {"a.py", "b.html", "c.js", "d.css"}
    (tmp_path / "f.py").write_text("x")
    (tmp_path / "b.html").unlink()
    assert changed_files(before, snapshot(tmp_path)) == 2


async def test_forward_and_list_changed_capability(harness: Harness) -> None:
    answer = await harness.start()
    assert answer["result"]["capabilities"]["tools"]["listChanged"] is True
    harness.send(call(2))
    assert (await harness.response(2))["result"]["content"][0]["text"] == "v1"


async def test_reload_replays_initialize(harness: Harness) -> None:
    await harness.start()
    (harness.state / "version").write_text("v2")
    harness.touch()
    await harness.list_changed()
    harness.send(call(3))
    assert (await harness.response(3))["result"]["content"][0]["text"] == "v2"
    log = harness.log()
    pids = list(dict.fromkeys(entry["pid"] for entry in log))
    assert len(pids) == 2
    second = [entry for entry in log if entry["pid"] == pids[1]]
    assert second[0]["method"] == "initialize"
    assert second[0]["id"].startswith(REPLAY_ID_PREFIX)
    assert second[1]["method"] == "notifications/initialized"


async def test_queue_during_restart(harness: Harness) -> None:
    await harness.start()
    (harness.state / "version").write_text("v2")
    (harness.state / "slow_start").write_text("1.0")
    harness.touch()
    await wait_until(lambda: not harness.proxy.ready.is_set())
    harness.send(call(4))
    answer = await harness.response(4)
    assert answer["result"]["content"][0]["text"] == "v2"


async def test_in_flight_request_gets_an_error(harness: Harness) -> None:
    await harness.start()
    harness.send(call(5, "slow"))
    await asyncio.sleep(0.2)
    harness.touch()
    answer = await harness.response(5)
    assert answer["error"]["message"] == RELOADED_MESSAGE
    await harness.list_changed()
    harness.send(call(6))
    assert "result" in await harness.response(6)


async def test_broken_child_then_recovery(harness: Harness) -> None:
    await harness.start()
    (harness.state / "broken").write_text("")
    harness.touch()
    await wait_until(lambda: harness.proxy.problem is not None)
    harness.send(call(7))
    error = (await harness.response(7))["error"]["message"]
    assert "cannot start after a code change" in error
    assert "exited with code 3" in error
    (harness.state / "broken").unlink()
    (harness.state / "version").write_text("v3")
    harness.touch()
    await harness.list_changed()
    harness.send(call(8))
    assert (await harness.response(8))["result"]["content"][0]["text"] == "v3"


async def test_client_eof_stops_the_child(harness: Harness) -> None:
    await harness.start()
    child = harness.proxy.child
    assert child is not None
    await harness.close()
    assert child.process.returncode is not None
    # A stop at client EOF is not a crash.
    assert harness.proxy.problem is None
