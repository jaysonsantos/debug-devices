"""Compare mode (--meter-local-decoder compare), stability, the dataset, and the evaluation (sevenseg/compare.py,
stability.py, dataset.py, evaluate.py). The local reading never changes the vision result."""

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import cv2
import httpx
import numpy as np
import pytest
from mcp import Client

from debug_devices_mcp.config import Settings
from debug_devices_mcp.meter_mode import MeterMode
from debug_devices_mcp.multimeter import MeterResult, MeterSource, MultimeterReading, check_reading
from debug_devices_mcp.server import Services, build_server
from debug_devices_mcp.sevenseg import compare as compare_module
from debug_devices_mcp.sevenseg.cli import main
from debug_devices_mcp.sevenseg.compare import (
    MISMATCH_NOTE,
    NO_CROP_NOTE,
    Field,
    LocalMeter,
    agrees,
    compare_local,
    differences,
    failed,
)
from debug_devices_mcp.sevenseg.constants import DATASET_DIR_NAME, LCD_SUFFIX, PROFILE_FILE_NAME, LocalDecoderMode
from debug_devices_mcp.sevenseg.dataset import Dataset, DatasetEntry, SavedImage, VisionFields, new_entry
from debug_devices_mcp.sevenseg.decode import Gray, decode_jpeg, decode_with_lcd, read_image, warp_size
from debug_devices_mcp.sevenseg.evaluate import evaluate, load_entries
from debug_devices_mcp.sevenseg.profile import ProfileError, Symbol, load_profile, save_profile
from debug_devices_mcp.sevenseg.reading import LocalReading, LocalStatus
from debug_devices_mcp.sevenseg.stability import SamplePlan, combine_readings, sample
from debug_devices_mcp.sevenseg.template import t21d_layout
from debug_devices_mcp.webcam import Crop, Webcam, WebcamOptions

from .conftest import FakeRunner, ok
from .sevenseg_synth import CROP_SIZE, Scene, crop_jpeg, profile_for
from .test_multimeter import READING, completion
from .test_server import FakePhone, make_services

LAYOUT = t21d_layout()
PROFILE = profile_for(LAYOUT)
VOLTS = {Symbol.VOLT, Symbol.DC}
FIVE_TEN = crop_jpeg(LAYOUT, Scene(" 510", point=1, symbols=VOLTS))
FIFTY_ONE = crop_jpeg(LAYOUT, Scene(" 510", point=2, symbols=VOLTS))
VISION_FIVE_TEN = {**READING, "display_text": "5.10", "value": 5.10}


def local(jpeg: bytes) -> LocalReading:
    return decode_jpeg(jpeg, PROFILE).reading


def jpeg_of(image: np.ndarray) -> bytes:
    ok_, data = cv2.imencode(".jpg", image)
    assert ok_
    return data.tobytes()


def vision(**changes: Any) -> VisionFields:
    return VisionFields.of_reading(MultimeterReading.model_validate({**VISION_FIVE_TEN, **changes}))


# region: stability


def test_frames_that_agree_are_stable() -> None:
    combined = combine_readings([local(FIVE_TEN), local(FIVE_TEN)], min_agree=2)
    assert (combined.status, combined.frames, combined.agreeing_frames) == (LocalStatus.READ, 2, 2)


def test_a_moved_point_is_unstable() -> None:
    combined = combine_readings([local(FIVE_TEN), local(FIFTY_ONE)], min_agree=2)
    assert combined.status is LocalStatus.UNSTABLE
    assert combined.agreeing_frames == 1
    assert "5.10 V dc_voltage, 51.0 V dc_voltage" in combined.problems[-1]


def test_an_ac_dc_change_is_unstable() -> None:
    ac = local(crop_jpeg(LAYOUT, Scene(" 510", point=1, symbols={Symbol.VOLT, Symbol.AC})))
    combined = combine_readings([local(FIVE_TEN), ac], min_agree=2)
    assert (ac.display_text, ac.mode) == ("5.10", MeterMode.AC_VOLTAGE)
    assert combined.status is LocalStatus.UNSTABLE
    assert "5.10 V dc_voltage, 5.10 V ac_voltage" in combined.problems[-1]


def test_too_few_readable_frames_are_unreadable() -> None:
    dark = local(crop_jpeg(LAYOUT, Scene(" 510", point=1, symbols=VOLTS, paper=150, ink=146)))
    combined = combine_readings([local(FIVE_TEN), dark], min_agree=2)
    assert dark.status is LocalStatus.UNREADABLE
    assert (combined.status, combined.readable, combined.value) == (LocalStatus.UNREADABLE, False, None)
    assert "1 of 2 frames are readable, 2 must agree" in combined.problems[-1]


def test_an_unreadable_frame_among_enough_readable_ones() -> None:
    dark = local(crop_jpeg(LAYOUT, Scene(" 510", point=1, symbols=VOLTS, paper=150, ink=146)))
    combined = combine_readings([local(FIVE_TEN), dark, local(FIVE_TEN)], min_agree=2)
    assert (combined.status, combined.display_text, combined.agreeing_frames) == (LocalStatus.READ, "5.10", 2)


def test_the_last_frames_decide() -> None:
    readings = [local(FIFTY_ONE), local(FIVE_TEN), local(FIVE_TEN), local(FIVE_TEN)]
    combined = combine_readings(readings, min_agree=3)
    assert (combined.display_text, combined.status, combined.agreeing_frames) == ("5.10", LocalStatus.READ, 3)


async def test_sampling_decodes_the_frames_of_the_plan() -> None:
    frames = [FIVE_TEN] * 5
    pauses: list[float] = []

    async def grab() -> bytes:
        return frames.pop()

    async def sleep(seconds: float) -> None:
        pauses.append(seconds)

    plan = SamplePlan(rate_hz=5, window=timedelta(seconds=1), min_agree=3)
    reading, decoded = await sample(grab, PROFILE, plan, sleep)
    assert (len(decoded), pauses) == (5, [0.2] * 4)
    assert (reading.display_text, reading.status) == ("5.10", LocalStatus.READ)


# endregion

# region: agreement


def test_the_same_reading_agrees() -> None:
    assert agrees(vision(), local(FIVE_TEN))


def test_a_misread_decimal_point_differs_in_the_point_only() -> None:
    assert differences(vision(display_text="51.0", value=51.0), local(FIVE_TEN)) == [Field.POINT]


def test_units_in_other_characters_agree() -> None:
    reading = local(crop_jpeg(LAYOUT, Scene("4700", point=0, symbols={Symbol.KILO, Symbol.OHM})))
    assert agrees(vision(display_text="4.700", value=4.7, unit="kohm", mode="resistance"), reading)


def test_mode_sign_and_unit_differences() -> None:
    reading = local(crop_jpeg(LAYOUT, Scene(" 510", point=1, negative=True, symbols={Symbol.MILLI, Symbol.VOLT})))
    assert differences(vision(mode="ac_voltage"), reading) == [Field.SIGN, Field.UNIT, Field.MODE]


def test_unreadable_never_agrees() -> None:
    assert not agrees(vision(readable=False), local(FIVE_TEN))


# endregion

# region: compare_local

LOCAL_FIELDS = {"local_reading", "local_agrees"}


def vision_result(display_text: str = "5.10", value: float = 5.10) -> MeterResult:
    reading = MultimeterReading.model_validate({**READING, "display_text": display_text, "value": value})
    return check_reading(reading)


def raw(display_text: str = "5.10", value: float = 5.10) -> MultimeterReading:
    return MultimeterReading.model_validate({**READING, "display_text": display_text, "value": value})


def meter_frames(*pairs: tuple[bytes, MultimeterReading]) -> list[tuple[bytes, MultimeterReading]]:
    return list(pairs)


def saved_files(directory: Path) -> dict[str, int]:
    """The files in the dataset folder by kind."""
    names = [path.name for path in directory.iterdir()] if directory.is_dir() else []
    return {
        "json": sum(name.endswith(".json") for name in names),
        "lcd": sum(name.endswith(LCD_SUFFIX) for name in names),
        "other": sum(not name.endswith((".json", LCD_SUFFIX)) for name in names),
    }


async def test_compare_adds_only_the_local_fields(tmp_path: Path) -> None:
    save_profile(PROFILE, tmp_path / PROFILE_FILE_NAME)
    result = vision_result()
    frames = meter_frames((FIVE_TEN, raw()), (FIVE_TEN, raw()))
    compared = await compare_local(result, MeterSource.WEBCAM, frames, crop_set=True, meter=LocalMeter(tmp_path))
    assert compared.local_reading is not None
    assert (compared.local_reading.display_text, compared.local_agrees) == ("5.10", True)
    assert compared.model_dump(exclude={"local_reading", "local_agrees"}) == result.model_dump(
        exclude={"local_reading", "local_agrees"}
    )


async def test_with_a_crop_the_dataset_keeps_only_the_warped_lcd(tmp_path: Path) -> None:
    save_profile(PROFILE, tmp_path / PROFILE_FILE_NAME)
    frames = meter_frames((FIVE_TEN, raw()), (FIVE_TEN, raw()))
    await compare_local(vision_result(), MeterSource.WEBCAM, frames, crop_set=True, meter=LocalMeter(tmp_path))
    dataset = Dataset(tmp_path / DATASET_DIR_NAME)
    assert saved_files(dataset.directory) == {"json": 2, "lcd": 2, "other": 0}
    for saved, image_path in dataset.entries():
        assert (saved.image, saved.note) == (SavedImage.LCD, None)
        assert image_path is not None
        image = cv2.imread(str(image_path), cv2.IMREAD_UNCHANGED)
        # The LCD area at the warp size: not the webcam crop (420x300).
        assert (image.shape[1], image.shape[0]) == warp_size(PROFILE.layout)


async def test_without_a_crop_no_image_is_saved(tmp_path: Path) -> None:
    save_profile(PROFILE, tmp_path / PROFILE_FILE_NAME)
    frames = meter_frames((FIVE_TEN, raw()), (FIVE_TEN, raw()))
    compared = await compare_local(
        vision_result(), MeterSource.WEBCAM, frames, crop_set=False, meter=LocalMeter(tmp_path)
    )
    assert compared.local_agrees is True
    dataset = Dataset(tmp_path / DATASET_DIR_NAME)
    assert saved_files(dataset.directory) == {"json": 2, "lcd": 0, "other": 0}
    assert [(saved.image, saved.note, path) for saved, path in dataset.entries()] == [
        (SavedImage.NONE, NO_CROP_NOTE, None)
    ] * 2


async def test_a_frame_that_does_not_match_the_profile_saves_no_image(tmp_path: Path) -> None:
    save_profile(PROFILE, tmp_path / PROFILE_FILE_NAME)
    # A full webcam frame (the crop was cleared): larger than the calibrated crop.
    full = np.full((720, 1280), 128, dtype=np.uint8)
    full[:300, :420] = read_image(FIVE_TEN)[:, :, 0]
    frame = jpeg_of(full)
    compared = await compare_local(
        vision_result(), MeterSource.WEBCAM, meter_frames((frame, raw())), crop_set=True, meter=LocalMeter(tmp_path)
    )
    assert compared.local_reading is not None
    assert compared.local_reading.status is LocalStatus.UNREADABLE
    dataset = Dataset(tmp_path / DATASET_DIR_NAME)
    assert saved_files(dataset.directory) == {"json": 1, "lcd": 0, "other": 0}
    assert [(saved.note, path) for saved, path in dataset.entries()] == [(MISMATCH_NOTE, None)]


async def test_a_disagreement_does_not_change_the_vision_result(tmp_path: Path) -> None:
    save_profile(PROFILE, tmp_path / PROFILE_FILE_NAME)
    result = vision_result("51.0", 51.0)
    frames = meter_frames((FIVE_TEN, raw("51.0", 51.0)))
    compared = await compare_local(result, MeterSource.WEBCAM, frames, crop_set=True, meter=LocalMeter(tmp_path))
    assert compared.local_agrees is False
    assert (compared.status, compared.value, compared.display_text) == (result.status, result.value, "51.0")


async def test_no_profile_says_so(tmp_path: Path) -> None:
    frames = meter_frames((FIVE_TEN, raw()))
    compared = await compare_local(
        vision_result(), MeterSource.WEBCAM, frames, crop_set=True, meter=LocalMeter(tmp_path)
    )
    assert compared.local_reading is not None
    assert compared.local_reading.status is LocalStatus.NO_PROFILE
    assert compared.local_agrees is False
    # N38: the log line never says "agrees" when local_agrees is false.
    line = LocalMeter(tmp_path).compare(vision_result(), frames, crop_set=True).log_line
    assert "agrees" not in line
    assert "not compared" in line


def test_the_log_line_names_the_differences(tmp_path: Path) -> None:
    save_profile(PROFILE, tmp_path / PROFILE_FILE_NAME)
    frames = meter_frames((FIVE_TEN, raw("51.0", 51.0)))
    line = LocalMeter(tmp_path, save_frames=False).compare(vision_result("51.0", 51.0), frames, crop_set=True).log_line
    assert "differs in point" in line


async def test_the_phone_source_is_not_compared(tmp_path: Path) -> None:
    save_profile(PROFILE, tmp_path / PROFILE_FILE_NAME)
    result = vision_result()
    frames = meter_frames((FIVE_TEN, raw()))
    assert await compare_local(result, MeterSource.PHONE, frames, crop_set=True, meter=LocalMeter(tmp_path)) is result


async def test_a_local_failure_keeps_the_vision_result_and_says_so(tmp_path: Path) -> None:
    save_profile(PROFILE, tmp_path / PROFILE_FILE_NAME)
    result = vision_result()
    frames = meter_frames((b"not a jpeg", raw()))
    compared = await compare_local(result, MeterSource.WEBCAM, frames, crop_set=True, meter=LocalMeter(tmp_path))
    assert compared.model_dump(exclude=LOCAL_FIELDS) == result.model_dump(exclude=LOCAL_FIELDS)
    assert compared.local_reading is not None
    assert compared.local_reading.status is LocalStatus.UNREADABLE
    assert compared.local_reading.problems == [
        "the local decoder failed: ImageDecodeError: the frame is not a JPEG or PNG image"
    ]
    assert compared.local_agrees is False
    assert "the local decoder failed" in failed(ValueError("x")).log_line


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda layout: layout["segments"].pop("g"), "missing segments: g"),
        (lambda layout: layout["points"].pop(), "2 decimal points for 4 digits"),
    ],
)
async def test_an_incomplete_profile_is_no_profile(tmp_path: Path, change: Any, message: str) -> None:
    data = json.loads(PROFILE.model_dump_json())
    change(data["layout"])
    path = tmp_path / PROFILE_FILE_NAME
    path.write_text(json.dumps(data))
    with pytest.raises(ProfileError, match=message):
        load_profile(path)

    frames = meter_frames((FIVE_TEN, raw()))
    compared = await compare_local(
        vision_result(), MeterSource.WEBCAM, frames, crop_set=True, meter=LocalMeter(tmp_path)
    )
    assert compared.local_reading is not None
    assert compared.local_reading.status is LocalStatus.NO_PROFILE
    assert message in compared.local_reading.problems[0]


# endregion

# region: dataset and evaluation


def entry(vision_fields: VisionFields, jpeg: bytes) -> tuple[DatasetEntry, Gray | None]:
    decoded, lcd = decode_with_lcd(read_image(jpeg), PROFILE)
    return new_entry(vision_fields, decoded.reading, decoded.regions, None, PROFILE.calibrated_at), lcd


def test_the_dataset_keeps_the_newest_entries(tmp_path: Path) -> None:
    dataset = Dataset(tmp_path, max_entries=3)
    added = [entry(vision(), FIVE_TEN) for _ in range(5)]
    for item, lcd in added:
        dataset.add(item, lcd)
    kept = [item.entry_id for item, _ in dataset.entries()]
    assert kept == [item.entry_id for item, _ in added[2:]]
    assert saved_files(tmp_path) == {"json": 3, "lcd": 3, "other": 0}


def test_old_webcam_frames_and_orphan_images_are_removed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    directory = tmp_path / DATASET_DIR_NAME
    directory.mkdir()
    # The format of the first version: a webcam crop as .jpg next to an entry without the image fields.
    old, _ = entry(vision(), FIVE_TEN)
    (directory / f"{old.entry_id}.json").write_text(old.model_dump_json(exclude={"image", "note"}))
    (directory / f"{old.entry_id}.jpg").write_bytes(FIVE_TEN)
    (directory / "0000-orphan.lcd.png").write_bytes(b"image without an entry")

    profile = tmp_path / PROFILE_FILE_NAME
    save_profile(PROFILE, profile)
    main(["evaluate", "--dataset", str(directory), "--redecode", "--profile", str(profile)])
    out = capsys.readouterr().out
    assert "removed 1 webcam frames of an older version" in out
    assert "decoded again: 0 of 1 entries" in out
    assert saved_files(directory) == {"json": 1, "lcd": 0, "other": 0}
    assert [(saved.entry_id, saved.image, path) for saved, path in Dataset(directory).entries()] == [
        (old.entry_id, SavedImage.NONE, None)
    ]


def test_a_new_entry_also_removes_old_webcam_frames(tmp_path: Path) -> None:
    (tmp_path / "0000-old.jpg").write_bytes(FIVE_TEN)
    item, lcd = entry(vision(), FIVE_TEN)
    Dataset(tmp_path).add(item, lcd)
    assert saved_files(tmp_path) == {"json": 1, "lcd": 1, "other": 0}


def test_the_evaluation_counts_each_field(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    dataset = Dataset(tmp_path / DATASET_DIR_NAME)
    for item, lcd in [
        entry(vision(), FIVE_TEN),
        entry(vision(), FIVE_TEN),
        entry(vision(display_text="51.0", value=51.0), FIVE_TEN),
        entry(vision(readable=False), FIVE_TEN),
    ]:
        dataset.add(item, lcd)
    evaluation = evaluate([item for item, _ in dataset.entries()])
    rates = {item.field: (item.agreed, item.compared) for item in evaluation.fields}
    assert (evaluation.entries, evaluation.both_readable, evaluation.all_agree) == (4, 3, 2)
    assert rates[Field.READABLE] == (3, 4)
    assert rates[Field.POINT] == (2, 3)
    assert rates[Field.DIGITS] == rates[Field.UNIT] == rates[Field.MODE] == (3, 3)

    profile = tmp_path / PROFILE_FILE_NAME
    save_profile(PROFILE, profile)
    main(["evaluate", "--dataset", str(dataset.directory), "--redecode", "--profile", str(profile)])
    out = capsys.readouterr().out
    assert "decoded again: 4 of 4 entries" in out
    assert "all fields agree: 2/3" in out
    assert "point: vision '2' (51.0 V dc_voltage), local '1' (5.10 V dc_voltage)" in out


def test_redecode_uses_the_regions_of_the_current_profile(tmp_path: Path) -> None:
    dataset = Dataset(tmp_path)
    item, lcd = entry(vision(), FIVE_TEN)
    dataset.add(item, lcd)
    # A profile with other corners (a new aspect): the saved LCD is scaled to its warp size.
    wider = PROFILE.model_copy(update={"layout": PROFILE.layout.model_copy(update={"aspect": 2.6})})
    entries, redecoded = load_entries(dataset, wider)
    assert redecoded == 1
    assert entries[0].local.display_text == "5.10"


# endregion

# region: through the tool (fake vision client)


# The crop that the synthetic frames stand for (the frames are already cropped).
CROP = Crop(x=0, y=0, width=CROP_SIZE[0], height=CROP_SIZE[1])


def services_for(settings: Settings, mode: LocalDecoderMode, answers: list[dict], crop: Crop | None = CROP) -> Services:
    queue = list(answers)
    vision_transport = httpx.MockTransport(lambda request: completion(json.dumps(queue.pop(0))))
    services = make_services(settings.model_copy(update={"meter_local_decoder": mode}), FakePhone(), vision_transport)
    runner = FakeRunner(lambda command: ok(FIVE_TEN))
    options = WebcamOptions(
        ffmpeg_path="ffmpeg", device=Path("/dev/video0"), warmup_frames=0, timeout=timedelta(1), crop=crop
    )
    services.webcam = Webcam(runner, options)
    return services


# Fields of the capture (new ids and times on each call) and of the local decoder.
SKIPPED_FIELDS = {"capture_id", "captured_at", "frames", "local_reading", "local_agrees"}
CAPTURE_ID = "<capture id>"


def vision_part(result: dict[str, Any]) -> dict[str, Any]:
    """The vision result without the capture ids and times (bench_notice names the capture id)."""
    kept = {key: value for key, value in result.items() if key not in SKIPPED_FIELDS}
    if kept.get("bench_notice"):
        kept["bench_notice"] = kept["bench_notice"].replace(result["capture_id"], CAPTURE_ID)
    return kept


async def read_tool(services: Services) -> dict[str, Any]:
    async with Client(build_server(services)) as client:
        result = await client.call_tool("multimeter_read", {"frames": 2})
    assert result.structured_content is not None
    return result.structured_content


@pytest.fixture
def state_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A private state folder with the calibrated profile."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    directory = compare_module.default_dir()
    save_profile(PROFILE, directory / PROFILE_FILE_NAME)
    return directory


@pytest.mark.parametrize("answers", [[VISION_FIVE_TEN] * 2, [VISION_FIVE_TEN, {**READING, "display_text": "51.0"}]])
async def test_compare_mode_does_not_change_multimeter_read(
    settings: Settings, state_home: Path, answers: list[dict]
) -> None:
    off = await read_tool(services_for(settings, LocalDecoderMode.OFF, answers))
    on = await read_tool(services_for(settings, LocalDecoderMode.COMPARE, answers))

    assert vision_part(on) == vision_part(off)
    assert [frame["display_text"] for frame in on["frames"]] == [frame["display_text"] for frame in off["frames"]]
    assert (off["local_reading"], off["local_agrees"]) == (None, None)
    assert on["local_reading"]["display_text"] == "5.10"
    assert on["local_reading"]["mode"] == MeterMode.DC_VOLTAGE
    # The local frames read 5.10 twice: it agrees only when every vision frame read 5.10 too.
    assert on["local_agrees"] is (on["status"] == "confirmed")
    assert on["local_reading"]["status"] == LocalStatus.READ
    assert saved_files(state_home / DATASET_DIR_NAME) == {"json": 2, "lcd": 2, "other": 0}


async def test_the_tool_without_a_crop_saves_no_image(settings: Settings, state_home: Path) -> None:
    result = await read_tool(services_for(settings, LocalDecoderMode.COMPARE, [VISION_FIVE_TEN] * 2, crop=None))
    assert result["local_agrees"] is True
    assert saved_files(state_home / DATASET_DIR_NAME) == {"json": 2, "lcd": 0, "other": 0}


async def test_off_mode_runs_no_local_code(
    settings: Settings, state_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fail(*args: object, **kwargs: object) -> MeterResult:
        raise AssertionError("the local decoder ran in off mode")

    monkeypatch.setattr("debug_devices_mcp.server.compare_local", fail)
    result = await read_tool(services_for(settings, LocalDecoderMode.OFF, [VISION_FIVE_TEN] * 2))
    assert result["status"] == "confirmed"
    assert not (state_home / DATASET_DIR_NAME).exists()


def test_the_setting_defaults_to_off(settings: Settings) -> None:
    assert settings.meter_local_decoder is LocalDecoderMode.OFF
    assert Settings.from_cli(["--meter-local-decoder", "compare"]).meter_local_decoder is LocalDecoderMode.COMPARE


# endregion
