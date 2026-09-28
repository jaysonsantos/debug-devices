"""`debug-devices-sevenseg`: calibrate the local meter decoder, read saved frames, and evaluate compare mode.

The CLI reads image files only. Get a webcam crop through the MCP tools (webcam_snapshot with save_path): never
from the monitor page.
"""

from pathlib import Path

import cv2
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, CliApp, CliSubCommand, PydanticBaseSettingsSource, SettingsConfigDict

from debug_devices_mcp.sevenseg.calibrate import annotate, calibrate
from debug_devices_mcp.sevenseg.constants import ANNOTATED_SUFFIX, DATASET_DIR_NAME, PROGRAM_NAME, TEMPLATE_T21D
from debug_devices_mcp.sevenseg.dataset import Dataset
from debug_devices_mcp.sevenseg.decode import DecodedFrame, decode_jpeg, read_image
from debug_devices_mcp.sevenseg.evaluate import evaluate, load_entries, report
from debug_devices_mcp.sevenseg.profile import default_dir, default_profile_path, load_profile, save_profile
from debug_devices_mcp.sevenseg.stability import combine_readings

MAX_DISAGREEMENTS = 20
MILLISECONDS_PER_SECOND = 1000


def describe(frame: DecodedFrame) -> str:
    reading = frame.reading
    lines = [
        f"{reading.display_text or '-'} {reading.unit} {reading.mode} flags={reading.flags} status={reading.status} "
        f"confidence={reading.confidence} contrast={reading.contrast} "
        f"({frame.elapsed.total_seconds() * MILLISECONDS_PER_SECOND:.1f} ms)"
    ]
    if frame.chars:
        lines.append(f"  digit positions: {frame.chars}")
    if reading.weak_regions:
        lines.append(f"  weak regions: {', '.join(reading.weak_regions)}")
    lines += [f"  problem: {problem}" for problem in reading.problems]
    return "\n".join(lines)


class Calibrate(BaseModel):
    """Find the LCD in a saved webcam crop, fit the template, and write the profile and an annotated image."""

    image: Path = Field(description="A webcam crop (JPEG or PNG), for example from webcam_snapshot save_path.")
    template: str = Field(default=TEMPLATE_T21D, description="The built-in LCD layout.")
    profile: Path = Field(default_factory=default_profile_path, description="Where to write the profile JSON.")
    annotated: Path | None = Field(default=None, description="The annotated image. Default: next to the profile.")
    fit: bool = Field(default=True, description="Move the digit row of the template to the digits in the image.")

    def cli_cmd(self) -> None:
        image = read_image(self.image.read_bytes())
        profile, decoded = calibrate(image, self.template, self.fit)
        save_profile(profile, self.profile)
        annotated = self.annotated or self.profile.with_suffix(ANNOTATED_SUFFIX)
        annotated.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(annotated), annotate(image, profile, decoded))
        print(f"profile: {self.profile}")
        print(f"annotated image (check every region): {annotated}")
        print(describe(decoded))


class Read(BaseModel):
    """Decode saved crops with the profile, and combine them (the frames must agree)."""

    images: list[Path] = Field(description="One or more crops of the same reading.")
    profile: Path = Field(default_factory=default_profile_path)
    min_agree: int | None = Field(default=None, ge=1, description="Frames that must agree. Default: all of them.")

    def cli_cmd(self) -> None:
        profile = load_profile(self.profile)
        frames = [decode_jpeg(path.read_bytes(), profile) for path in self.images]
        for path, frame in zip(self.images, frames, strict=True):
            print(f"{path}: {describe(frame)}")
        if len(frames) > 1:
            combined = combine_readings([frame.reading for frame in frames], self.min_agree or len(frames))
            print(
                f"combined: {combined.display_text or '-'} {combined.unit} {combined.mode} status={combined.status} "
                f"({combined.agreeing_frames}/{combined.frames} frames agree)"
            )


class Evaluate(BaseModel):
    """The agreement rate per field between the vision model and the local decoder in the compare-mode dataset."""

    dataset: Path = Field(default_factory=lambda: default_dir() / DATASET_DIR_NAME)
    redecode: bool = Field(default=False, description="Decode the saved frames again with the current profile.")
    profile: Path = Field(default_factory=default_profile_path)
    max_disagreements: int = Field(default=MAX_DISAGREEMENTS, ge=0)

    def cli_cmd(self) -> None:
        profile = load_profile(self.profile) if self.redecode else None
        entries = load_entries(Dataset(self.dataset), profile)
        print(report(evaluate(entries), self.max_disagreements))


class SevensegCli(BaseSettings):
    """The local 7-segment decoder of the multimeter (compare mode only)."""

    model_config = SettingsConfigDict(cli_prog_name=PROGRAM_NAME, cli_kebab_case=True, cli_implicit_flags=True)

    calibrate: CliSubCommand[Calibrate] = Field(description="Write the profile from one saved webcam crop.")
    read: CliSubCommand[Read] = Field(description="Decode saved crops with the profile.")
    evaluate: CliSubCommand[Evaluate] = Field(description="Agreement with the vision model in the dataset.")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Flags only: a variable such as READ in the environment must not choose a subcommand.
        return (init_settings,)

    def cli_cmd(self) -> None:
        CliApp.run_subcommand(self)


def main(args: list[str] | None = None) -> None:
    CliApp.run(SevensegCli, cli_args=args)
