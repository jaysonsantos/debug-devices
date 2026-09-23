#!/usr/bin/env python3
"""MCP tool tests over stdio against scripts/fake_phone.py, scripts/fake_adb.py, and a mock OpenRouter.

It needs the `mcp` package, so run it with uv from the repo root:

    uv run python scripts/qa_mcp_stdio.py
    uv run python scripts/qa_mcp_stdio.py --skip-webcam

It never calls the real adb and never sends a request to OpenRouter. The key in `.env` is replaced
by a dummy value in the environment of the MCP server. Exit code 0 means every case passed.
See docs/qa.md section 2.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import shutil
import struct
import subprocess
import sys
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, TextIO

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult, ImageContent, TextContent

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fake_adb
import fake_phone

# region: constants

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRATCH = Path("/tmp/dd-qa")  # set from --scratch in main()
SHARED_WEBCAM_LOG = "using the frames of the monitor"
# No monitor window (and no scrcpy or shared webcam stream) during the tests.
SERVER_ARGS = ("--no-ui",)
FAKE_ADB = REPO_ROOT / "scripts" / "fake_adb.py"
HOST = "127.0.0.1"
EPHEMERAL_PORT = 0
DEFAULT_MODEL = "openai/gpt-6-luna"
DUMMY_API_KEY = "qa-dummy-key-not-a-secret"
MISSING_WEBCAM = "/dev/video99"
SHORT_TIMEOUT_SECONDS = "3"
TOOL_TIMEOUT_SECONDS = 60.0
ZOOM_STEP_FACTOR = 1.5
# phone_snapshot and webcam_snapshot downscale the long side to this by default. 0 means full size.
DEFAULT_MAX_SIDE = 1568
FULL_SIZE = 0
ZOOM_TOLERANCE = 1e-3
CHAT_COMPLETIONS_PATH = "/chat/completions"
JPEG_MIME = "image/jpeg"
JPEG_SOI = b"\xff\xd8"
JPEG_SOF_MARKERS = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}
EXPECTED_TOOLS = frozenset(
    {
        "phone_connect",
        "phone_status",
        "phone_zoom",
        "phone_torch",
        "phone_snapshot",
        "webcam_snapshot",
        "multimeter_read",
    }
)
# Only these variables pass from this shell to the MCP server, so the test does not depend on the shell.
PASSED_ENV = ("PATH", "HOME", "USER", "LANG", "TMPDIR", "XDG_RUNTIME_DIR", "UV_CACHE_DIR")

MOCK_READING: dict[str, Any] = {
    "readable": True,
    "value": 4.98,
    "unit": "V",
    "display_text": "4.98",
    "mode": "dc_voltage",
    "range": "auto",
    "flags": ["AUTO"],
    "confidence": 0.93,
    "notes": "",
}

# endregion: constants

# region: mock openrouter


@dataclass
class MockOpenRouter:
    """Chat completions mock. `content` is the assistant message it returns."""

    content: str = json.dumps(MOCK_READING)
    status: HTTPStatus = HTTPStatus.OK
    requests: list[dict[str, Any]] = field(default_factory=list)
    auth_seen: list[bool] = field(default_factory=list)

    def serve(self) -> ThreadingHTTPServer:
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass

            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                # Record only that a bearer token came in. Never store the token.
                mock.auth_seen.append(self.headers.get("Authorization", "").startswith("Bearer "))
                if not self.path.endswith(CHAT_COMPLETIONS_PATH):
                    self.send_response(HTTPStatus.NOT_FOUND)
                    self.end_headers()
                    return
                mock.requests.append(json.loads(body))
                payload = json.dumps({"choices": [{"message": {"role": "assistant", "content": mock.content}}]})
                self.send_response(mock.status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload.encode())

        return start(ThreadingHTTPServer((HOST, EPHEMERAL_PORT), Handler))


# endregion: mock openrouter

# region: harness


def start(server: ThreadingHTTPServer) -> ThreadingHTTPServer:
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def port_of(server: ThreadingHTTPServer) -> int:
    return server.server_address[1]


def set_phone_mode(server: ThreadingHTTPServer, config: fake_phone.FakeConfig) -> None:
    server.RequestHandlerClass.camera = fake_phone.FakeCamera(config)


class CaseFailed(AssertionError):
    pass


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise CaseFailed(message)


def text_of(result: CallToolResult) -> str:
    return " ".join(block.text for block in result.content if isinstance(block, TextContent))


def expect_ok(result: CallToolResult) -> dict[str, Any]:
    expect(not result.is_error, f"tool error: {text_of(result)[:300]}")
    if result.structured_content is not None:
        data = result.structured_content
        # A tool that returns a model has its fields under "result" or at the top level.
        return data.get("result", data) if isinstance(data, dict) else {"result": data}
    return {}


def expect_tool_error(result: CallToolResult, *needles: str) -> str:
    text = text_of(result)
    expect(result.is_error, f"expected a tool error, got: {text[:300]}")
    for needle in needles:
        expect(needle in text, f"tool error does not contain {needle!r}: {text[:300]}")
    return text


def expect_zoom(actual: float, wanted: float, what: str) -> None:
    expect(abs(actual - wanted) <= ZOOM_TOLERANCE, f"{what}: zoom_ratio {actual}, expected {wanted}")


def jpeg_size(data: bytes) -> tuple[int, int]:
    index = len(JPEG_SOI)
    while index + 4 <= len(data):
        marker, length = data[index + 1], struct.unpack(">H", data[index + 2 : index + 4])[0]
        if marker in JPEG_SOF_MARKERS:
            height, width = struct.unpack(">HH", data[index + 5 : index + 9])
            return width, height
        index += 2 + length
    raise CaseFailed("JPEG has no SOF segment")


def expect_image(result: CallToolResult) -> tuple[int, int, int]:
    expect(not result.is_error, f"tool error: {text_of(result)[:300]}")
    images = [block for block in result.content if isinstance(block, ImageContent)]
    expect(len(images) == 1, f"expected one image, got {len(images)}")
    expect(images[0].mime_type == JPEG_MIME, f"mimeType is {images[0].mime_type!r}")
    data = base64.b64decode(images[0].data)
    expect(data.startswith(JPEG_SOI), "image is not a JPEG")
    width, height = jpeg_size(data)
    return width, height, len(data)


def mcp_env(overrides: dict[str, str]) -> dict[str, str]:
    env = {name: os.environ[name] for name in PASSED_ENV if name in os.environ}
    env.update(overrides)
    return env


@dataclass(frozen=True)
class CaseResult:
    session: str
    name: str
    passed: bool
    detail: str


Case = Callable[[ClientSession], Awaitable[str]]


def stderr_log(label: str) -> Path:
    return SCRATCH / f"mcp_{label}.stderr.log"


async def run_session(label: str, env: dict[str, str], cases: list[tuple[str, Case]]) -> list[CaseResult]:
    """One MCP server process. Its stderr goes to `stderr_log(label)`, so a case can read the server log."""
    uv = shutil.which("uv") or "uv"
    params = StdioServerParameters(
        command=uv, args=["run", "--quiet", "debug-devices-mcp", *SERVER_ARGS], env=mcp_env(env), cwd=REPO_ROOT
    )
    with stderr_log(label).open("w") as errlog:
        return await _run_cases(label, params, cases, errlog)


async def _run_cases(
    label: str, params: StdioServerParameters, cases: list[tuple[str, Case]], errlog: TextIO
) -> list[CaseResult]:
    results: list[CaseResult] = []
    async with stdio_client(params, errlog=errlog) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        for name, case in cases:
            try:
                detail = await asyncio.wait_for(case(session), TOOL_TIMEOUT_SECONDS)
            except (CaseFailed, TimeoutError) as err:
                results.append(CaseResult(label, name, False, f"{type(err).__name__}: {err}"))
            else:
                results.append(CaseResult(label, name, True, detail))
    return results


def call(session: ClientSession, tool: str, **arguments: Any) -> Awaitable[CallToolResult]:
    return session.call_tool(tool, arguments, read_timeout_seconds=TOOL_TIMEOUT_SECONDS)


# endregion: harness

# region: cases


@dataclass
class World:
    phone: ThreadingHTTPServer
    openrouter: MockOpenRouter
    adb_log: Path
    snapshot: bytes


def served_snapshot(world: World) -> bytes:
    """The fake phone adds an EXIF orientation segment for the current rotation (auto, upright: 0 degrees)."""
    return fake_phone.with_exif_orientation(world.snapshot, fake_phone.DEGREES_TO_EXIF_ORIENTATION[0])


def fake_cases(world: World, with_webcam: bool) -> list[tuple[str, Case]]:  # noqa: PLR0915 (one closure per case)
    async def list_tools(session: ClientSession) -> str:
        tools = (await session.list_tools()).tools
        names = {tool.name for tool in tools}
        expect(names >= EXPECTED_TOOLS, f"missing tools: {sorted(EXPECTED_TOOLS - names)}")
        for tool in tools:
            expect(bool(tool.description), f"{tool.name} has no description")
            expect(tool.input_schema.get("type") == "object", f"{tool.name} input schema is not an object")
        return f"{len(tools)} tools"

    async def connect(session: ClientSession) -> str:
        data = expect_ok(await call(session, "phone_connect"))
        expect(data.get("serial") == fake_adb.FAKE_SERIAL, f"serial {data.get('serial')!r}")
        log = world.adb_log.read_text()
        expect(f"-s {fake_adb.FAKE_SERIAL} forward tcp:{port_of(world.phone)} tcp:8765" in log, f"adb log: {log!r}")
        return f"started_app={data.get('started_app')}"

    async def status(session: ClientSession) -> str:
        data = expect_ok(await call(session, "phone_status"))
        expect(
            set(data) >= {"zoom_ratio", "min_zoom_ratio", "max_zoom_ratio", "torch_enabled", "has_flash_unit"},
            str(data),
        )
        return json.dumps(data)

    async def zoom(session: ClientSession) -> str:
        low = expect_ok(await call(session, "phone_zoom", ratio=1.0))
        expect_zoom(low["zoom_ratio"], 1.0, "ratio 1")
        expect_zoom(expect_ok(await call(session, "phone_zoom", step="in"))["zoom_ratio"], ZOOM_STEP_FACTOR, "step in")
        expect_zoom(expect_ok(await call(session, "phone_zoom", step="out"))["zoom_ratio"], 1.0, "step out")
        expect_zoom(expect_ok(await call(session, "phone_zoom", ratio=100))["zoom_ratio"], 8.0, "ratio 100 clamp")
        expect_tool_error(await call(session, "phone_zoom"), "exactly one")
        expect_tool_error(await call(session, "phone_zoom", ratio=2.0, step="in"), "exactly one")
        expect((await call(session, "phone_zoom", step="sideways")).is_error, "step 'sideways' is not a tool error")
        return "ratio, step, clamp, argument errors"

    async def torch(session: ClientSession) -> str:
        expect(expect_ok(await call(session, "phone_torch", enabled=True))["torch_enabled"] is True, "torch on")
        expect(expect_ok(await call(session, "phone_torch", enabled=False))["torch_enabled"] is False, "torch off")
        return "on, off"

    async def snapshot(session: ClientSession) -> str:
        width, height, size = expect_image(await call(session, "phone_snapshot"))
        expect(max(width, height) <= DEFAULT_MAX_SIDE, f"default snapshot is {width}x{height}, over {DEFAULT_MAX_SIDE}")
        _, _, full_size = expect_image(await call(session, "phone_snapshot", max_side=FULL_SIZE))
        served = served_snapshot(world)
        expect(full_size == len(served), f"max_side=0: size {full_size} != served {len(served)}")
        return f"default {width}x{height} {size} bytes, max_side=0 {full_size} bytes"

    async def snapshot_save(session: ClientSession) -> str:
        target = world.adb_log.parent / "mcp_phone_snapshot.jpg"
        target.unlink(missing_ok=True)
        expect_image(await call(session, "phone_snapshot", save_path=str(target)))
        expect(target.read_bytes() == served_snapshot(world), "saved file differs from the snapshot")
        return str(target)

    def phone_error_case(config: fake_phone.FakeConfig, tool: str, code: str, **arguments: Any) -> Case:
        async def case(session: ClientSession) -> str:
            set_phone_mode(world.phone, config)
            try:
                return expect_tool_error(await call(session, tool, **arguments), code)[:120]
            finally:
                set_phone_mode(world.phone, fake_phone.FakeConfig(snapshot=world.snapshot))

        return case

    async def multimeter_mock(session: ClientSession) -> str:
        world.openrouter.content = json.dumps(MOCK_READING)
        before = len(world.openrouter.requests)
        result = await call(session, "multimeter_read", include_image=True)
        data = expect_ok(result)
        expect(data.get("value") == MOCK_READING["value"], f"reading {data}")
        expect(data.get("mode") == MOCK_READING["mode"], f"reading {data}")
        expect(any(isinstance(b, ImageContent) for b in result.content), "include_image=True gave no image")
        request = world.openrouter.requests[before]
        expect(request["model"] == DEFAULT_MODEL, f"model {request['model']!r}")
        fmt = request.get("response_format", {})
        expect(fmt.get("type") == "json_schema", f"response_format {fmt}")
        expect(fmt.get("json_schema", {}).get("strict") is True, "json_schema.strict is not true")
        parts = request["messages"][-1]["content"]
        urls = [p["image_url"]["url"] for p in parts if p.get("type") == "image_url"]
        expect(len(urls) == 1 and urls[0].startswith("data:image/jpeg;base64,"), "no JPEG data URL")
        expect(world.openrouter.auth_seen[-1], "no bearer token")
        return f"model={request['model']} value={data['value']} {data['unit']}"

    async def multimeter_bad_output(session: ClientSession) -> str:
        world.openrouter.content = "The meter shows about five volts."
        try:
            return expect_tool_error(await call(session, "multimeter_read"), "valid reading")[:120]
        finally:
            world.openrouter.content = json.dumps(MOCK_READING)

    async def multimeter_fenced(session: ClientSession) -> str:
        world.openrouter.content = "```json\n" + json.dumps(MOCK_READING) + "\n```"
        try:
            expect_ok(await call(session, "multimeter_read"))
            return "fenced JSON accepted"
        finally:
            world.openrouter.content = json.dumps(MOCK_READING)

    async def webcam(session: ClientSession) -> str:
        width, height, size = expect_image(await call(session, "webcam_snapshot"))
        # SharedWebcam logs one line when /dev/video0 is busy and it uses the frames of the running monitor.
        path = "shared (monitor)" if SHARED_WEBCAM_LOG in stderr_log("fake").read_text() else "local"
        return f"{width}x{height} {size} bytes, path={path}"

    async def phone_down(session: ClientSession) -> str:
        world.phone.shutdown()
        world.phone.server_close()
        text = expect_tool_error(await call(session, "phone_status"))
        # The server must stay up after the error.
        await session.list_tools()
        return text[:160]

    cases: list[tuple[str, Case]] = [
        ("list_tools", list_tools),
        ("phone_connect", connect),
        ("phone_status", status),
        ("phone_zoom", zoom),
        ("phone_torch", torch),
        ("phone_snapshot", snapshot),
        ("phone_snapshot_save_path", snapshot_save),
        (
            "torch_no_flash",
            phone_error_case(fake_phone.FakeConfig(has_flash_unit=False), "phone_torch", "no_flash_unit", enabled=True),
        ),
        (
            "camera_not_ready",
            phone_error_case(fake_phone.FakeConfig(ready=False), "phone_status", "camera_not_ready"),
        ),
        (
            "capture_failed",
            phone_error_case(fake_phone.FakeConfig(capture_fails=True), "phone_snapshot", "capture_failed"),
        ),
    ]
    if with_webcam:
        cases += [
            ("multimeter_mock", multimeter_mock),
            ("multimeter_bad_output", multimeter_bad_output),
            ("multimeter_fenced", multimeter_fenced),
            ("webcam_snapshot", webcam),
        ]
    cases.append(("phone_down", phone_down))
    return cases


def no_key_cases() -> list[tuple[str, Case]]:
    async def no_key(session: ClientSession) -> str:
        return expect_tool_error(await call(session, "multimeter_read"), "OPENROUTER_API_KEY")[:120]

    return [("multimeter_no_key", no_key)]


def missing_webcam_cases() -> list[tuple[str, Case]]:
    async def missing(session: ClientSession) -> str:
        return expect_tool_error(await call(session, "webcam_snapshot"), MISSING_WEBCAM)[:160]

    return [("webcam_missing", missing)]


def several_devices_cases() -> list[tuple[str, Case]]:
    async def several(session: ClientSession) -> str:
        return expect_tool_error(await call(session, "phone_connect"), "several adb devices")[:200]

    return [("adb_several_devices_no_serial", several)]


# endregion: cases

# region: stdout hygiene


def check_stdout_is_jsonrpc(env: dict[str, str]) -> CaseResult:
    """Send initialize and tools/list on raw stdio. Every stdout line must be JSON."""
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "qa", "version": "0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    stdin = "".join(json.dumps(m) + "\n" for m in messages)
    proc = subprocess.run(
        [shutil.which("uv") or "uv", "run", "--quiet", "debug-devices-mcp", *SERVER_ARGS],
        input=stdin,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=mcp_env(env),
        timeout=TOOL_TIMEOUT_SECONDS,
        check=False,
    )
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    bad = []
    for line in lines:
        try:
            json.loads(line)
        except json.JSONDecodeError:
            bad.append(line)
    passed = bool(lines) and not bad
    detail = f"{len(lines)} JSON lines" if passed else f"non-JSON stdout: {bad[:3]} stderr: {proc.stderr[-300:]}"
    return CaseResult("raw-stdio", "stdout_is_jsonrpc", passed, detail)


# endregion: stdout hygiene


async def main_async(args: argparse.Namespace, adb_log: Path, snapshot: bytes) -> int:

    phone = start(fake_phone.make_server(HOST, EPHEMERAL_PORT, fake_phone.FakeConfig(snapshot=snapshot), quiet=True))
    openrouter = MockOpenRouter()
    openrouter_server = openrouter.serve()
    base_env = {
        "DEBUG_DEVICES_ADB_PATH": str(FAKE_ADB),
        "DEBUG_DEVICES_ADB_SERIAL": fake_adb.FAKE_SERIAL,
        "DEBUG_DEVICES_LOCAL_FORWARD_PORT": str(port_of(phone)),
        "DEBUG_DEVICES_OPENROUTER_BASE_URL": f"http://{HOST}:{port_of(openrouter_server)}",
        "DEBUG_DEVICES_APP_START_TIMEOUT": SHORT_TIMEOUT_SECONDS,
        "DEBUG_DEVICES_VISION_MODEL": DEFAULT_MODEL,
        "OPENROUTER_API_KEY": DUMMY_API_KEY,
        "FAKE_ADB_LOG": str(adb_log),
    }
    world = World(phone, openrouter, adb_log, snapshot)

    results: list[CaseResult] = [check_stdout_is_jsonrpc(base_env)]
    results += await run_session("fake", base_env, fake_cases(world, not args.skip_webcam))
    results += await run_session("no-key", {**base_env, "OPENROUTER_API_KEY": ""}, no_key_cases())
    results += await run_session(
        "no-webcam", {**base_env, "DEBUG_DEVICES_WEBCAM": MISSING_WEBCAM}, missing_webcam_cases()
    )
    if args.real_adb_several_devices:
        # Real adb, empty serial: the server must refuse before it sends any command to a device.
        env = {**base_env, "DEBUG_DEVICES_ADB_PATH": "adb", "DEBUG_DEVICES_ADB_SERIAL": ""}
        results += await run_session("real-adb", env, several_devices_cases())
    openrouter_server.shutdown()

    for result in results:
        mark = "PASS" if result.passed else "FAIL"
        print(f"[{result.session}] {mark} {result.name}  ({result.detail})")
    failed = sum(not r.passed for r in results)
    print(f"{len(results) - failed}/{len(results)} cases passed")
    return 0 if failed == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scratch", default="/tmp/dd-qa", help="directory for logs and saved files")
    parser.add_argument("--snapshot", help="JPEG that the fake phone serves")
    parser.add_argument("--skip-webcam", action="store_true", help="skip the cases that read /dev/video0")
    parser.add_argument(
        "--real-adb-several-devices",
        action="store_true",
        help="also run phone_connect with the real adb and no serial. It must refuse (only `adb devices` runs).",
    )
    args = parser.parse_args()
    global SCRATCH  # noqa: PLW0603 (one run, one scratch directory)
    scratch = SCRATCH = Path(args.scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    adb_log = scratch / "fake_adb.log"
    adb_log.unlink(missing_ok=True)
    snapshot = Path(args.snapshot).read_bytes() if args.snapshot else fake_phone.TEST_JPEG
    return asyncio.run(main_async(args, adb_log, snapshot))


if __name__ == "__main__":
    sys.exit(main())
