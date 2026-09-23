"""Settings from CLI flags, environment variables, the repo .env file, and defaults."""

from datetime import timedelta
from pathlib import Path
from typing import Annotated

from pydantic import BeforeValidator, Field, SecretStr
from pydantic_settings import BaseSettings, CliApp, SettingsConfigDict

from debug_devices_mcp.constants import ENV_FILE, PROGRAM_NAME, defaults, env


def _parse_seconds(value: object) -> object:
    """Read a plain number (also as text from a flag or a variable) as seconds."""
    if isinstance(value, str):
        try:
            return timedelta(seconds=float(value))
        except ValueError:
            return value
    return value


type Seconds = Annotated[timedelta, BeforeValidator(_parse_seconds)]


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
    webcam: Path = Field(default=defaults.WEBCAM, description="V4L2 device of the webcam at the multimeter.")
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

    @classmethod
    def from_cli(cls, args: list[str] | None = None) -> Settings:
        return CliApp.run(cls, cli_args=args)
