"""Compare mode (--meter-local-decoder compare), stability, the dataset, and the evaluation (sevenseg/compare.py,
stability.py, dataset.py, evaluate.py). The local reading never changes the vision result."""

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp import Client

from debug_devices_mcp.config import Settings
from debug_devices_mcp.meter_mode import MeterMode
from debug_devices_mcp.multimeter import MeterResult, MeterSource, MultimeterReading, check_reading
from debug_devices_mcp.server import Services, build_server
from debug_devices_mcp.sevenseg import compare as compare_module
from debug_devices_mcp.sevenseg.cli import main
from debug_devices_mcp.sevenseg.compare import Field, LocalMeter, agrees, compare_local, differences
from debug_devices_mcp.sevenseg.constants import DATASET_DIR_NAME, PROFILE_FILE_NAME, LocalDecoderMode
from debug_devices_mcp.sevenseg.dataset import Dataset, VisionFields, new_entry
from debug_devices_mcp.sevenseg.decode import decode_jpeg
from debug_devices_mcp.sevenseg.evaluate import evaluate
from debug_devices_mcp.sevenseg.profile import Symbol, save_profile
from debug_devices_mcp.sevenseg.reading import LocalReading, LocalStatus
from debug_devices_mcp.sevenseg.stability import SamplePlan, combine_readings, sample
from debug_devices_mcp.sevenseg.template import t21d_layout
from debug_devices_mcp.webcam import Webcam, WebcamOptions

from .conftest import FakeRunner, ok
from .sevenseg_synth import Scene, crop_jpeg, profile_for
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
    assert "5.10, 51.0" in combined.problems[-1]


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


def vision_result(display_text: str = "5.10", value: float = 5.10) -> MeterResult:
    reading = MultimeterReading.model_validate({**READING, "display_text": display_text, "value": value})
    return check_reading(reading)


def raw(display_text: str = "5.10", value: float = 5.10) -> MultimeterReading:
    return MultimeterReading.model_validate({**READING, "display_text": display_text, "value": value})


async def test_compare_adds_only_the_local_fields(tmp_path: Path) -> None:
    save_profile(PROFILE, tmp_path / PROFILE_FILE_NAME)
    result = vision_result()
    compared = await compare_local(
        result, MeterSource.WEBCAM, [FIVE_TEN, FIVE_TEN], [raw(), raw()], LocalMeter(tmp_path)
    )
    assert compared.local_reading is not None
    assert (compared.local_reading.display_text, compared.local_agrees) == ("5.10", True)
    assert compared.model_dump(exclude={"local_reading", "local_agrees"}) == result.model_dump(
        exclude={"local_reading", "local_agrees"}
    )
    assert len(Dataset(tmp_path / DATASET_DIR_NAME).entry_paths()) == 2


async def test_a_disagreement_does_not_change_the_vision_result(tmp_path: Path) -> None:
    save_profile(PROFILE, tmp_path / PROFILE_FILE_NAME)
    result = vision_result("51.0", 51.0)
    compared = await compare_local(result, MeterSource.WEBCAM, [FIVE_TEN], [raw("51.0", 51.0)], LocalMeter(tmp_path))
    assert compared.local_agrees is False
    assert (compared.status, compared.value, compared.display_text) == (result.status, result.value, "51.0")


async def test_no_profile_says_so(tmp_path: Path) -> None:
    compared = await compare_local(vision_result(), MeterSource.WEBCAM, [FIVE_TEN], [raw()], LocalMeter(tmp_path))
    assert compared.local_reading is not None
    assert compared.local_reading.status is LocalStatus.NO_PROFILE
    assert compared.local_agrees is False


async def test_the_phone_source_is_not_compared(tmp_path: Path) -> None:
    save_profile(PROFILE, tmp_path / PROFILE_FILE_NAME)
    result = vision_result()
    assert await compare_local(result, MeterSource.PHONE, [FIVE_TEN], [raw()], LocalMeter(tmp_path)) is result


async def test_a_local_failure_leaves_the_result(tmp_path: Path) -> None:
    save_profile(PROFILE, tmp_path / PROFILE_FILE_NAME)
    result = vision_result()
    assert await compare_local(result, MeterSource.WEBCAM, [b"not a jpeg"], [raw()], LocalMeter(tmp_path)) is result


# endregion

# region: dataset and evaluation


def entry(vision_fields: VisionFields, jpeg: bytes) -> tuple[Any, bytes]:
    decoded = decode_jpeg(jpeg, PROFILE)
    return new_entry(vision_fields, decoded.reading, decoded.regions, None, PROFILE.calibrated_at), jpeg


def test_the_dataset_keeps_the_newest_entries(tmp_path: Path) -> None:
    dataset = Dataset(tmp_path, max_entries=3)
    added = [entry(vision(), FIVE_TEN) for _ in range(5)]
    for item, jpeg in added:
        dataset.add(item, jpeg)
    kept = [item.entry_id for item, _ in dataset.entries()]
    assert kept == [item.entry_id for item, _ in added[2:]]
    assert len(list(tmp_path.glob("*.jpg"))) == 3


def test_the_evaluation_counts_each_field(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    dataset = Dataset(tmp_path / DATASET_DIR_NAME)
    for item, jpeg in [
        entry(vision(), FIVE_TEN),
        entry(vision(), FIVE_TEN),
        entry(vision(display_text="51.0", value=51.0), FIVE_TEN),
        entry(vision(readable=False), FIVE_TEN),
    ]:
        dataset.add(item, jpeg)
    evaluation = evaluate([item for item, _ in dataset.entries()])
    rates = {item.field: (item.agreed, item.compared) for item in evaluation.fields}
    assert (evaluation.entries, evaluation.both_readable, evaluation.all_agree) == (4, 3, 2)
    assert rates[Field.READABLE] == (3, 4)
    assert rates[Field.POINT] == (2, 3)
    assert rates[Field.DIGITS] == rates[Field.UNIT] == rates[Field.MODE] == (3, 3)

    save_profile(PROFILE, tmp_path / PROFILE_FILE_NAME)
    main(
        ["evaluate", "--dataset", str(dataset.directory), "--redecode", "--profile", str(tmp_path / PROFILE_FILE_NAME)]
    )
    out = capsys.readouterr().out
    assert "all fields agree: 2/3" in out
    assert "point: vision '2' (51.0 V dc_voltage), local '1' (5.10 V dc_voltage)" in out


# endregion

# region: through the tool (fake vision client)


def services_for(settings: Settings, mode: LocalDecoderMode, frames: list[bytes], answers: list[dict]) -> Services:
    queue = list(answers)
    vision_transport = httpx.MockTransport(lambda request: completion(json.dumps(queue.pop(0))))
    services = make_services(settings.model_copy(update={"meter_local_decoder": mode}), FakePhone(), vision_transport)
    webcam_frames = list(frames)
    runner = FakeRunner(lambda command: ok(webcam_frames.pop(0)))
    services.webcam = Webcam(
        runner, WebcamOptions(ffmpeg_path="ffmpeg", device=Path("/dev/video0"), warmup_frames=0, timeout=timedelta(1))
    )
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
    off = await read_tool(services_for(settings, LocalDecoderMode.OFF, [FIVE_TEN] * 2, answers))
    on = await read_tool(services_for(settings, LocalDecoderMode.COMPARE, [FIVE_TEN] * 2, answers))

    assert vision_part(on) == vision_part(off)
    assert [frame["display_text"] for frame in on["frames"]] == [frame["display_text"] for frame in off["frames"]]
    assert (off["local_reading"], off["local_agrees"]) == (None, None)
    assert on["local_reading"]["display_text"] == "5.10"
    assert on["local_reading"]["mode"] == MeterMode.DC_VOLTAGE
    # The local frames read 5.10 twice: it agrees only when every vision frame read 5.10 too.
    assert on["local_agrees"] is (on["status"] == "confirmed")
    assert on["local_reading"]["status"] == LocalStatus.READ
    assert len(Dataset(state_home / DATASET_DIR_NAME).entry_paths()) == 2


async def test_off_mode_runs_no_local_code(
    settings: Settings, state_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fail(*args: object) -> MeterResult:
        raise AssertionError("the local decoder ran in off mode")

    monkeypatch.setattr("debug_devices_mcp.server.compare_local", fail)
    result = await read_tool(services_for(settings, LocalDecoderMode.OFF, [FIVE_TEN] * 2, [VISION_FIVE_TEN] * 2))
    assert result["status"] == "confirmed"
    assert not (state_home / DATASET_DIR_NAME).exists()


def test_the_setting_defaults_to_off(settings: Settings) -> None:
    assert settings.meter_local_decoder is LocalDecoderMode.OFF
    assert Settings.from_cli(["--meter-local-decoder", "compare"]).meter_local_decoder is LocalDecoderMode.COMPARE


# endregion
