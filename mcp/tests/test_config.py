from datetime import timedelta
from pathlib import Path

import pytest

from debug_devices_mcp.config import Settings
from debug_devices_mcp.constants import defaults


def test_defaults(settings: Settings) -> None:
    assert settings.vision_model == "openai/gpt-6-luna"
    assert settings.webcam == Path("/dev/video0")
    assert settings.adb_serial == ""
    assert settings.openrouter_api_key is None
    assert settings.phone_http_timeout == defaults.PHONE_HTTP_TIMEOUT


def test_environment_and_flags(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    monkeypatch.setenv("DEBUG_DEVICES_WEBCAM", "/dev/video2")
    monkeypatch.setenv("DEBUG_DEVICES_VISION_TIMEOUT", "12.5")

    loaded = Settings.from_cli(["--adb-serial", "ABC123", "--local-forward-port", "19000"])

    assert loaded.openrouter_api_key is not None
    assert loaded.openrouter_api_key.get_secret_value() == "secret"
    assert loaded.webcam == Path("/dev/video2")
    assert loaded.vision_timeout == timedelta(seconds=12.5)
    assert loaded.adb_serial == "ABC123"
    assert loaded.local_forward_port == 19000
