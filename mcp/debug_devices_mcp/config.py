"""Settings from CLI flags, environment variables, the repo .env file, and defaults."""

from datetime import timedelta
from pathlib import Path
from typing import Annotated

from pydantic import AliasChoices, BeforeValidator, Field, SecretStr
from pydantic_settings import BaseSettings, CliApp, NoDecode, SettingsConfigDict

from debug_devices_mcp.board.constants import defaults as board_defaults
from debug_devices_mcp.board.constants import env as board_env
from debug_devices_mcp.constants import ENV_FILE, PROGRAM_NAME, defaults, env
from debug_devices_mcp.ui.constants import defaults as ui_defaults
from debug_devices_mcp.ui.constants import screen as ui_screen
from debug_devices_mcp.webcam import Crop


def _parse_seconds(value: object) -> object:
    """Read a plain number (also as text from a flag or a variable) as seconds."""
    if isinstance(value, str):
        try:
            return timedelta(seconds=float(value))
        except ValueError:
            return value
    return value


type Seconds = Annotated[timedelta, BeforeValidator(_parse_seconds)]


def _parse_crop(value: object) -> object:
    """Read `x,y,w,h` text. Empty text means no crop."""
    if isinstance(value, str):
        return Crop.parse(value) if value.strip() else None
    return value


type CropSetting = Annotated[Crop | None, NoDecode, BeforeValidator(_parse_crop)]


class Settings(BaseSettings):
    """Every field is a `--kebab-case` flag and a `DEBUG_DEVICES_*` variable. Durations are seconds."""

    model_config = SettingsConfigDict(
        env_prefix=env.PREFIX,
        env_file=ENV_FILE,
        extra="ignore",
        cli_prog_name=PROGRAM_NAME,
        cli_kebab_case=True,
        cli_implicit_flags=True,
    )

    openrouter_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=env.OPENROUTER_API_KEY.lower(),
        description="OpenRouter API key for the multimeter vision model.",
    )
    vision_model: str = Field(default=defaults.VISION_MODEL, description="OpenRouter model id with image input.")
    openrouter_base_url: str = defaults.OPENROUTER_BASE_URL
    meter_model: str = Field(
        default="",
        description='Make and model of the multimeter, for example "PROSTER T21D". The vision prompt names it.',
    )
    webcam: Path = Field(default=defaults.WEBCAM, description="V4L2 device of the webcam at the multimeter.")
    webcam_crop: CropSetting = Field(
        default=None,
        description="Crop x,y,w,h in pixels. Only this part of the webcam frame leaves the PC (tools and OpenRouter).",
    )
    webcam_warmup_frames: int = Field(default=defaults.WEBCAM_WARMUP_FRAMES, ge=0)
    adb_serial: str = Field(default="", description="ADB serial of the phone. Empty means the only device.")
    local_forward_port: int = Field(default=defaults.LOCAL_FORWARD_PORT, gt=0, le=defaults.MAX_PORT)
    adb_path: str = defaults.ADB
    ffmpeg_path: str = defaults.FFMPEG
    phone_http_timeout: Seconds = defaults.PHONE_HTTP_TIMEOUT
    phone_snapshot_timeout: Seconds = defaults.PHONE_SNAPSHOT_TIMEOUT
    app_start_timeout: Seconds = defaults.APP_START_TIMEOUT
    adb_timeout: Seconds = defaults.ADB_TIMEOUT
    webcam_timeout: Seconds = defaults.WEBCAM_TIMEOUT
    vision_timeout: Seconds = defaults.VISION_TIMEOUT
    poll_interval: Seconds = defaults.POLL_INTERVAL
    ui: bool = Field(
        default=True,
        description="Monitor window: a local web page with the webcam, the phone, and the tool calls. "
        "--no-ui also turns off the phone screen, scrcpy, and the shared webcam stream.",
    )
    ui_port: int = Field(
        default=ui_defaults.PORT, ge=0, le=defaults.MAX_PORT, description="Port on 127.0.0.1. Busy: a free port."
    )
    ui_open_browser: bool = Field(default=True, description="Open the monitor page in a new Firefox window at start.")
    phone_screen: bool = Field(
        default=True, description="Show the phone screen in the monitor page after phone_connect (scrcpy server)."
    )
    phone_screen_max_size: int = Field(default=ui_screen.MAX_SIZE, gt=0, description="Long edge of the screen video.")
    scrcpy_window: bool = Field(default=False, description="Also open a separate scrcpy window after phone_connect.")
    scrcpy_path: str = ui_defaults.SCRCPY
    scrcpy_server_path: Path = Field(
        default=ui_screen.SERVER_PATH, description="The scrcpy server that goes to the phone for the page screen."
    )
    scrcpy_server_version: str = Field(
        default="", description="Version of the scrcpy server. Empty: from `scrcpy --version`. Must match the server."
    )
    # region: boardview. The environment names have no DEBUG_DEVICES_ prefix (see .env.example).
    obv_dump_path: str = Field(
        default=board_defaults.DUMP_BIN,
        validation_alias=AliasChoices("obv_dump_path", board_env.DUMP_BIN.lower()),
        description="The obv-dump binary (boardview parser). Default: obv-dump on PATH.",
    )
    boardview_dump_timeout: Seconds = board_defaults.DUMP_TIMEOUT
    # Keys for encrypted boardview formats. Prefer the environment: a flag shows in the process list.
    boardview_fz_key: SecretStr | None = Field(default=None, validation_alias=board_env.FZ_KEY.lower())
    boardview_cae_key: SecretStr | None = Field(default=None, validation_alias=board_env.CAE_KEY.lower())
    boardview_xzz_key: SecretStr | None = Field(default=None, validation_alias=board_env.XZZ_KEY.lower())
    # endregion: boardview

    @classmethod
    def from_cli(cls, args: list[str] | None = None) -> Settings:
        return CliApp.run(cls, cli_args=args)
