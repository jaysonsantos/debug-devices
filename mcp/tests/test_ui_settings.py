from pathlib import Path

import pytest
from pydantic import ValidationError

from debug_devices_mcp.ui.settings import EffectiveSettings, ScreenRotation, SettingsStore, UiSettings, state_dir
from debug_devices_mcp.webcam import Crop

START = EffectiveSettings(vision_model="openai/gpt-6-luna", webcam_warmup_frames=10, webcam_crop=None)


def test_state_dir_uses_xdg_state_home() -> None:
    assert state_dir({"XDG_STATE_HOME": "/x/state", "HOME": "/home/u"}) == Path("/x/state/debug-devices")


def test_state_dir_falls_back_to_home() -> None:
    assert state_dir({"HOME": "/home/u"}) == Path("/home/u/.local/state/debug-devices")


def test_store_round_trip(tmp_path: Path) -> None:
    store = SettingsStore.in_dir(tmp_path / "new")
    assert store.load() == UiSettings()
    saved = UiSettings(vision_model="x/y", webcam_warmup_frames=0, webcam_crop=Crop(x=1, y=2, width=3, height=4))
    store.save(saved)
    assert store.path == tmp_path / "new" / "ui-settings.json"
    assert SettingsStore(store.path).load() == saved
    assert not list(store.path.parent.glob("*.tmp"))


def test_broken_file_gives_empty_settings(tmp_path: Path) -> None:
    store = SettingsStore.in_dir(tmp_path)
    store.path.write_text("{not json")
    assert store.load() == UiSettings()


def test_saved_values_win_over_start_values() -> None:
    crop = Crop(x=0, y=0, width=10, height=10)
    effective = START.with_saved(UiSettings(webcam_warmup_frames=0, webcam_crop=crop))
    assert effective == EffectiveSettings(vision_model="openai/gpt-6-luna", webcam_warmup_frames=0, webcam_crop=crop)
    assert START.with_saved(UiSettings()) == START


def test_screen_rotation_is_saved_and_defaults_to_auto(tmp_path: Path) -> None:
    assert START.screen_rotation == ScreenRotation.AUTO
    store = SettingsStore.in_dir(tmp_path)
    store.save(UiSettings(screen_rotation=ScreenRotation.DEG_270))
    assert '"screen_rotation": "270"' in store.path.read_text()
    loaded = store.load()
    assert loaded.screen_rotation == ScreenRotation.DEG_270
    assert START.with_saved(loaded).screen_rotation == ScreenRotation.DEG_270
    assert START.with_saved(UiSettings()).screen_rotation == ScreenRotation.AUTO


def test_unknown_screen_rotation_is_refused() -> None:
    with pytest.raises(ValidationError):
        UiSettings.model_validate({"screen_rotation": "45"})
