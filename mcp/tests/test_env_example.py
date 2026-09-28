"""`.env.example` lists every variable of Settings, and no DEBUG_DEVICES_ variable that nothing reads (C18 of QA
round 4, AGENTS.md: ".env.example lists every variable")."""

import re
from pathlib import Path

from debug_devices_mcp.config import Settings

ENV_EXAMPLE = Path(__file__).resolve().parents[2] / ".env.example"
PREFIX = "DEBUG_DEVICES_"
ASSIGNMENT = re.compile(r"^([A-Z][A-Z0-9_]*)=", re.MULTILINE)


def names_of(field_name: str) -> set[str]:
    """The variable names that set this field: its aliases, and the prefixed field name."""
    field = Settings.model_fields[field_name]
    alias = field.validation_alias
    aliases = {str(choice).upper() for choice in getattr(alias, "choices", [alias])} if alias is not None else set()
    return aliases | {f"{PREFIX}{field_name.upper()}"}


def test_every_setting_is_in_the_example() -> None:
    listed = set(ASSIGNMENT.findall(ENV_EXAMPLE.read_text()))
    missing = [name for name in Settings.model_fields if not names_of(name) & listed]
    assert missing == []


def test_no_unknown_variable_in_the_example() -> None:
    known = set().union(*(names_of(name) for name in Settings.model_fields))
    listed = ASSIGNMENT.findall(ENV_EXAMPLE.read_text())
    assert [name for name in listed if name.startswith(PREFIX) and name not in known] == []


def test_the_example_is_a_valid_env_file(settings: Settings) -> None:
    # A user copies it to .env: every line, also the empty ones, must parse, and the defaults stay the defaults.
    from_example = Settings(_env_file=ENV_EXAMPLE)  # type: ignore[call-arg]
    assert from_example.adb_serial == ""
    assert from_example.webcam_crop is None
    assert from_example.local_forward_port == settings.local_forward_port
    assert from_example.scrcpy_window is False
