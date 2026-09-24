"""The user's instructions file: server instructions at start and the bench_instructions tool."""

import os
from pathlib import Path
from typing import cast

import pytest
from mcp import Client
from mcp.server.mcpserver import MCPServer

from debug_devices_mcp.config import Settings
from debug_devices_mcp.instructions import (
    DEFAULT_INSTRUCTIONS_FILE,
    EVIDENCE_RULES,
    EXAMPLE_FILE_NAME,
    HEADER,
    MAX_SERVER_INSTRUCTIONS_BYTES,
    cap_text,
    read_instructions,
    server_instructions,
)
from debug_devices_mcp.server import build_server
from debug_devices_mcp.ui.monitor import Monitor
from debug_devices_mcp.ui.tools import register_monitor_tools

from .test_server import FakePhone, make_services, no_vision

GUIDE = "Tool guide."
USER_TEXT = "# My bench\n\nDevice under test: a laptop mainboard.\nNever go above 20 V.\n"


def test_server_instructions_with_file(tmp_path: Path) -> None:
    path = tmp_path / "instructions.md"
    path.write_text(USER_TEXT)

    text = server_instructions(path, GUIDE)

    assert text.startswith(HEADER)
    assert "First call bench_instructions and follow it." in text
    assert GUIDE in text
    assert str(path) in text
    assert text.endswith(USER_TEXT.strip())


def test_server_instructions_without_file(tmp_path: Path) -> None:
    path = tmp_path / "missing.md"

    text = server_instructions(path, GUIDE)

    assert text.startswith(HEADER)
    assert EXAMPLE_FILE_NAME in text
    assert str(path) in text
    result = read_instructions(path)
    assert result.exists is False
    assert result.modified is None
    assert result.how_to_create is not None


def test_server_instructions_are_capped(tmp_path: Path) -> None:
    path = tmp_path / "instructions.md"
    # 3-byte characters, so a byte cut can fall inside a character.
    path.write_text("€" * MAX_SERVER_INSTRUCTIONS_BYTES)

    text = server_instructions(path, GUIDE)

    assert "cut at 8 KiB" in text
    user_part = text.split(f"User instructions from {path}:\n\n", 1)[1]
    body = user_part.split("\n\n", 1)[1]
    assert len(body.encode()) <= MAX_SERVER_INSTRUCTIONS_BYTES
    assert set(body) == {"€"}
    # bench_instructions still returns all of it.
    assert len(read_instructions(path).content) == MAX_SERVER_INSTRUCTIONS_BYTES


def test_cap_text_keeps_short_text() -> None:
    assert cap_text("abc", 10) == ("abc", False)
    assert cap_text("abcdef", 3) == ("abc", True)


def test_setting_default_env_and_flag(settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # The fixture moves the file into tmp_path; the default is instructions.md at the repo root, like .env.
    assert Settings.model_fields["instructions_file"].default == DEFAULT_INSTRUCTIONS_FILE
    assert DEFAULT_INSTRUCTIONS_FILE.parent == Path(__file__).resolve().parents[2]
    assert settings.instructions_file.parent == tmp_path
    monkeypatch.setenv("DEBUG_DEVICES_INSTRUCTIONS", str(tmp_path / "a.md"))
    assert Settings.from_cli([]).instructions_file == tmp_path / "a.md"
    assert Settings.from_cli(["--instructions-file", "/x/b.md"]).instructions_file == Path("/x/b.md")
    # An empty variable, as in .env.example, means the default file.
    monkeypatch.setenv("DEBUG_DEVICES_INSTRUCTIONS", "")
    assert Settings.from_cli([]).instructions_file == DEFAULT_INSTRUCTIONS_FILE


def test_unreadable_path_is_not_an_error(tmp_path: Path) -> None:
    result = read_instructions(tmp_path)  # a directory
    assert result.exists is False
    assert result.how_to_create is not None
    assert "cannot read" in result.how_to_create
    assert "cannot read" in server_instructions(tmp_path, GUIDE)


async def test_initialize_and_fresh_tool_read(settings: Settings, tmp_path: Path) -> None:
    path = tmp_path / "instructions.md"
    path.write_text(USER_TEXT)
    settings.instructions_file = path
    services = make_services(settings, FakePhone(), no_vision())

    async with Client(build_server(services)) as client:
        instructions = client.instructions
        first = await client.call_tool("bench_instructions", {})
        path.write_text("Changed while the server runs.\n")
        os.utime(path, (1_900_000_000, 1_900_000_000))
        second = await client.call_tool("bench_instructions", {})
        path.unlink()
        missing = await client.call_tool("bench_instructions", {})

    assert instructions is not None
    assert instructions.startswith(HEADER)
    assert "Never go above 20 V." in instructions
    assert first.structured_content is not None
    assert first.structured_content["content"] == USER_TEXT
    assert first.structured_content["path"] == str(path)
    assert second.structured_content is not None
    assert second.structured_content["content"] == "Changed while the server runs.\n"
    assert second.structured_content["modified"].startswith("2030-03-17")
    assert missing.structured_content is not None
    assert missing.structured_content["exists"] is False
    assert EXAMPLE_FILE_NAME in missing.structured_content["how_to_create"]


async def test_bench_start_description_points_at_the_instructions() -> None:
    server = MCPServer("test")
    # Registering the tools does not use the monitor; a stand-in is enough to read the descriptions.
    register_monitor_tools(server, cast(Monitor, object()))

    async with Client(server) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}

    assert "bench_instructions" in (tools["bench_start"].description or "")


def test_bench_instructions_carry_the_evidence_rules(tmp_path: Path) -> None:
    # Codex keeps only the header of the server instructions, so the tool result must carry the rules.
    assert read_instructions(tmp_path / "missing.md").evidence_rules == EVIDENCE_RULES
    assert "user's cockpit" in HEADER
