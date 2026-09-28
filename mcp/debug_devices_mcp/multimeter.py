"""Read a multimeter display from a webcam frame with an OpenRouter vision model."""

import base64
from datetime import timedelta
from enum import StrEnum
from typing import Any, Literal

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, SecretStr, ValidationError

from debug_devices_mcp.constants import CONTENT_TYPE_HEADER, ERROR_BODY_PREVIEW_CHARS, JSON_CONTENT_TYPE, openrouter

# region: reading


class MeterMode(StrEnum):
    DC_VOLTAGE = "dc_voltage"
    AC_VOLTAGE = "ac_voltage"
    DC_CURRENT = "dc_current"
    AC_CURRENT = "ac_current"
    RESISTANCE = "resistance"
    CONTINUITY = "continuity"
    DIODE = "diode"
    CAPACITANCE = "capacitance"
    FREQUENCY = "frequency"
    TEMPERATURE = "temperature"
    DUTY_CYCLE = "duty_cycle"
    OTHER = "other"


class MeterSource(StrEnum):
    """The camera that sees the multimeter."""

    WEBCAM = "webcam"
    PHONE = "phone"


class MultimeterReading(BaseModel):
    """Every field is required and nullable where it can be empty, so the schema works in strict mode."""

    model_config = ConfigDict(extra="forbid")

    readable: bool = Field(description="False when the display is not visible, blurred, or cut off.")
    # The defaults are for local callers only: the strict schema that goes to the model requires every field.
    digits: str | None = Field(
        default=None,
        description="The digit characters on the LCD from left to right, without the sign and the decimal point, "
        "for example '1415' for '1.415'. Null when no digits are readable (blank display or OL).",
    )
    digits_before_point: int | None = Field(
        default=None,
        description="How many digits are to the left of the decimal point, for example 1 for '1.415' and 2 for "
        "'14.15'. Null when the LCD shows no decimal point.",
    )
    value: float | None = Field(
        description="Numeric value in `unit`, with the sign: the digits with the decimal point where the LCD shows it. "
        "Null when not readable or when the display shows OL."
    )
    unit: str = Field(
        description='Unit as shown, with its prefix, e.g. "mV", "V", "kΩ", "µF", "°C", "Hz", "%". '
        '"unknown" when the unit symbol is not readable. Never take the unit from the dial alone.'
    )
    display_text: str = Field(description="Exact digits and symbols on the display, e.g. '-0.123' or 'OL'.")
    mode: MeterMode = Field(description="Measurement mode from the dial position and the display symbols.")
    range: str | None = Field(description='Selected range, e.g. "20V", or "auto". Null when unknown.')
    flags: list[str] = Field(description='Indicators on the display, e.g. "HOLD", "AUTO", "REL", "low battery".')
    confidence: float = Field(ge=0, le=1, description="Confidence in the value, from 0 to 1.")
    notes: str = Field(description="Short remarks: glare, a blurred digit, a reason for low confidence. Can be empty.")


SYSTEM_PROMPT = (
    "You read a digital multimeter from one photo. Report only what the photo shows.\n"
    "- Read every digit, the decimal point, the sign, and the unit with its prefix on the LCD.\n"
    "- Find the decimal point first: a small dot at the bottom of the LCD between two digits. Say between which "
    "digits it is: put the digits, left to right, in digits, and the number of digits to the left of the point in "
    "digits_before_point (null when there is no point). display_text and value must agree with them.\n"
    "- The unit symbol and the annunciators on the LCD decide the mode: V or mV with the DC bar or '⎓' is "
    "dc_voltage, with '~' or AC is ac_voltage, A/mA/µA the same way for current, Ω/kΩ/MΩ is resistance, "
    "the diode symbol is diode, the buzzer symbol is continuity, nF/µF is capacitance, Hz is frequency, "
    "% is duty_cycle, °C/°F is temperature.\n"
    "- The rotary dial only helps. On many meters one dial position has several functions "
    "(for example resistance, diode, and continuity). Do not take the mode from the dial alone.\n"
    '- If you cannot read the unit symbol, put "unknown" in unit, say so in notes, give the mode that the dial '
    "and the digits suggest, and set confidence to 0.5 or lower.\n"
    "- For an overload, put 'OL' in display_text and null in value.\n"
    "- If the display is blank or you cannot read the digits, set readable to false and say why in notes.\n"
    "Answer with one JSON object that matches the schema. No other text."
)
USER_PROMPT = "Read this multimeter."
METER_MODEL_PROMPT = "The meter is a {meter_model}. Use what you know about its display and dial."


METER_COUNTS_PROMPT = (
    "For volts and amperes, its display has {counts} counts: at most {digits} digits. The lowest range can show a "
    "blank for the leading digit: do not add a zero for it."
)


def display_digits(counts: int) -> int:
    """The digits of a display with this many counts: 6000 counts show 4 digits (at most 5999)."""
    return len(str(counts - 1))


def user_prompt(meter_model: str, counts: int = 0) -> str:
    parts = [USER_PROMPT]
    if meter_model.strip():
        parts.append(METER_MODEL_PROMPT.format(meter_model=meter_model.strip()))
    if counts > 0:
        parts.append(METER_COUNTS_PROMPT.format(counts=counts, digits=display_digits(counts)))
    return " ".join(parts)


# Keywords that OpenAI strict mode does not accept, or that add nothing for the model.
# pydantic still checks the bounds (for example `confidence` in [0, 1]) when it parses the answer.
UNSUPPORTED_KEYWORDS = frozenset({"title", "default", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"})
DEFS_KEY = "$defs"
REF_KEY = "$ref"
REF_PREFIX = "#/$defs/"
ANY_OF_KEY = "anyOf"
TYPE_KEY = "type"
NULL_TYPE = "null"
OBJECT_TYPE = "object"
PROPERTIES_KEY = "properties"
REQUIRED_KEY = "required"
ADDITIONAL_PROPERTIES_KEY = "additionalProperties"


def _nullable_type(options: list[Any]) -> list[str] | None:
    """`anyOf: [{type: x}, {type: null}]` becomes `[x, "null"]`. Other unions stay as they are."""
    if not all(isinstance(option, dict) and set(option) == {TYPE_KEY} for option in options):
        return None
    types = [option[TYPE_KEY] for option in options]
    if NULL_TYPE not in types or not all(isinstance(kind, str) for kind in types):
        return None
    return types


def _strict(node: Any, defs: dict[str, Any]) -> Any:
    if isinstance(node, list):
        return [_strict(item, defs) for item in node]
    if not isinstance(node, dict):
        return node
    if REF_KEY in node:
        target = defs[node[REF_KEY].removeprefix(REF_PREFIX)]
        siblings = {key: value for key, value in node.items() if key != REF_KEY}
        return _strict({**target, **siblings}, defs)
    result = {key: _strict(value, defs) for key, value in node.items() if key not in UNSUPPORTED_KEYWORDS}
    if ANY_OF_KEY in result and (types := _nullable_type(result[ANY_OF_KEY])) is not None:
        del result[ANY_OF_KEY]
        result[TYPE_KEY] = types
    if result.get(TYPE_KEY) == OBJECT_TYPE and PROPERTIES_KEY in result:
        result[REQUIRED_KEY] = list(result[PROPERTIES_KEY])
        result[ADDITIONAL_PROPERTIES_KEY] = False
    return result


def reading_json_schema() -> dict[str, Any]:
    """JSON schema for OpenAI strict structured outputs.

    No `$ref` or `$defs` (enums are inline), nullable fields as `type: [x, "null"]`, every property required,
    and `additionalProperties: false` on every object.
    """
    schema = MultimeterReading.model_json_schema()
    defs = schema.pop(DEFS_KEY, {})
    return _strict(schema, defs)


# endregion: reading

# region: check
# The model output is not a measurement yet. The check below compares the unit symbol with the mode (and with the
# mode that the current test expects). Only a consistent, confident reading gives a numeric value.


class MeterStatus(StrEnum):
    # Unit and mode agree, the confidence is high enough: `value` is the measurement.
    CONFIRMED = "confirmed"
    # The unit symbol is not readable, the confidence is low, or the mode is "other": no numeric conclusion.
    UNCERTAIN = "uncertain"
    # The unit does not fit the mode, or the reading does not fit the expected mode: no numeric conclusion.
    DISPUTED = "disputed"
    # The display is not readable at all.
    UNREADABLE = "unreadable"


class UnitFamily(StrEnum):
    RESISTANCE = "resistance"
    VOLTAGE = "voltage"
    CURRENT = "current"
    CAPACITANCE = "capacitance"
    FREQUENCY = "frequency"
    TEMPERATURE = "temperature"
    PERCENT = "percent"
    # The explicit unknown state: no unit symbol, or one that the check does not know.
    UNKNOWN = "unknown"


# A confirmed measurement needs at least this model confidence.
MIN_CONFIRMED_CONFIDENCE = 0.7
UNKNOWN_UNIT_WORDS = frozenset({"", "unknown", "?", "none", "n/a", "-"})
SI_PREFIXES = "pnumkM"
MICRO_SIGNS = str.maketrans({"μ": "u", "µ": "u"})
# Unit suffixes after the SI prefix; the longest suffix first.
UNIT_SUFFIXES: tuple[tuple[str, UnitFamily], ...] = (
    ("ohms", UnitFamily.RESISTANCE),
    ("ohm", UnitFamily.RESISTANCE),
    ("\u03a9", UnitFamily.RESISTANCE),  # Greek capital omega
    ("\u2126", UnitFamily.RESISTANCE),  # OHM SIGN
    ("Hz", UnitFamily.FREQUENCY),
    ("°C", UnitFamily.TEMPERATURE),
    ("°F", UnitFamily.TEMPERATURE),
    ("degC", UnitFamily.TEMPERATURE),
    ("degF", UnitFamily.TEMPERATURE),
    ("V", UnitFamily.VOLTAGE),
    ("A", UnitFamily.CURRENT),
    ("F", UnitFamily.CAPACITANCE),
    ("%", UnitFamily.PERCENT),
)
# The unit families that fit each mode. Continuity can show Ω or only a buzzer symbol; diode mode shows V.
MODE_FAMILIES: dict[MeterMode, frozenset[UnitFamily]] = {
    MeterMode.DC_VOLTAGE: frozenset({UnitFamily.VOLTAGE}),
    MeterMode.AC_VOLTAGE: frozenset({UnitFamily.VOLTAGE}),
    MeterMode.DC_CURRENT: frozenset({UnitFamily.CURRENT}),
    MeterMode.AC_CURRENT: frozenset({UnitFamily.CURRENT}),
    MeterMode.RESISTANCE: frozenset({UnitFamily.RESISTANCE}),
    MeterMode.CONTINUITY: frozenset({UnitFamily.RESISTANCE}),
    MeterMode.DIODE: frozenset({UnitFamily.VOLTAGE}),
    MeterMode.CAPACITANCE: frozenset({UnitFamily.CAPACITANCE}),
    MeterMode.FREQUENCY: frozenset({UnitFamily.FREQUENCY}),
    MeterMode.TEMPERATURE: frozenset({UnitFamily.TEMPERATURE}),
    MeterMode.DUTY_CYCLE: frozenset({UnitFamily.PERCENT}),
    MeterMode.OTHER: frozenset(),
}
OVERLOAD_TEXTS = frozenset({"OL", "0L", "-OL", "-0L"})
# Words in the model's notes that mean a dim or faint LCD (the backlight or the webcam exposure can fix it).
DIM_WORDS = ("dim", "faint", "dark", "low contrast", "washed out", "barely", "not lit", "unlit")
DIM_HINT = (
    "The LCD is dim: turn on the meter backlight ({backlight}), or raise the webcam exposure or brightness "
    "(webcam_controls), then call multimeter_read again."
)
GENERIC_BACKLIGHT = "the backlight key of the meter"


def dim_hint(reading: MultimeterReading, meter_model: str) -> str | None:
    """A hint when the model says that the LCD is dim or faint."""
    notes = reading.notes.casefold()
    if not any(word in notes for word in DIM_WORDS):
        return None
    backlight = f"the {meter_model.strip()} backlight key" if meter_model.strip() else GENERIC_BACKLIGHT
    return DIM_HINT.format(backlight=backlight)


NEW_FRAME_REQUEST = (
    "Take a new frame that shows the LCD symbols and the dial together (move or zoom the camera, then call "
    "multimeter_read again). If the symbols stay unclear, ask the user to confirm the physical meter mode."
)


def unit_parts(unit: str) -> tuple[UnitFamily, str]:
    """The family and the SI prefix ("", "m", "k", "u", ...) of a unit symbol as the model read it."""
    text = unit.strip().translate(MICRO_SIGNS).replace(" ", "")
    if text.casefold() in UNKNOWN_UNIT_WORDS:
        return UnitFamily.UNKNOWN, ""
    for suffix, family in UNIT_SUFFIXES:
        if text.endswith(suffix):
            prefix = text.removesuffix(suffix)
            if len(prefix) <= 1 and all(char in SI_PREFIXES for char in prefix):
                return family, prefix
    return UnitFamily.UNKNOWN, ""


def unit_family(unit: str) -> UnitFamily:
    """The family of a unit symbol as the model read it. Anything that the check does not know is UNKNOWN."""
    return unit_parts(unit)[0]


def is_overload(display_text: str) -> bool:
    return display_text.replace(" ", "").replace(".", "").upper() in OVERLOAD_TEXTS


# region: digits and decimal point
# A misplaced decimal point is the most common misread ("14.15" for "1.415"). The model gives the digits and the point
# position as separate fields; they must agree with the LCD text and the value.

DISAGREE_PROBLEM = "the digits, the decimal point, and the value of the model do not agree"
VALUE_TOLERANCE = 1e-9
DECIMAL_BASE = 10
MINUS_SIGNS = "-\u2212"  # hyphen-minus and MINUS SIGN


def signature(display_text: str) -> tuple[str, int | None]:
    """The digits of the LCD text and the position of the decimal point in them (None: no point)."""
    text = display_text.replace(" ", "")
    digits = "".join(char for char in text if char.isdigit())
    point = text.find(".")
    return digits, (len([char for char in text[:point] if char.isdigit()]) if point >= 0 else None)


def signed_value(display_text: str, digits: str, point: int | None) -> float:
    """The number that the digits and the point make, with the sign of the LCD text."""
    magnitude = int(digits) / DECIMAL_BASE ** (len(digits) - point) if point is not None else float(int(digits))
    first_digit = next((index for index, char in enumerate(display_text) if char.isdigit()), len(display_text))
    negative = any(char in MINUS_SIGNS for char in display_text[:first_digit])
    return -magnitude if negative else magnitude


def consistency_problems(reading: MultimeterReading) -> list[str]:
    """The model's digits, point, LCD text, and value must tell the same number."""
    if not reading.readable or is_overload(reading.display_text):
        return []
    digits, point = signature(reading.display_text)
    if not digits:
        return []
    details = []
    if reading.digits is not None:
        field_digits = "".join(char for char in reading.digits if char.isdigit())
        if (field_digits, reading.digits_before_point) != (digits, point):
            details.append(
                f"LCD text {reading.display_text!r} has digits {digits} with {point} before the point, "
                f"the fields say {field_digits or 'none'} with {reading.digits_before_point}"
            )
    if reading.value is not None:
        shown = signed_value(reading.display_text, digits, point)
        if abs(reading.value - shown) > VALUE_TOLERANCE * max(1.0, abs(shown)):
            details.append(f"LCD text {reading.display_text!r} is {shown:g}, the value is {reading.value:g}")
    return [f"{DISAGREE_PROBLEM} ({'; '.join(details)})"] if details else []


AUTO_WORD = "auto"
# A leading zero with the point after this many digits or more cannot be on an auto-ranging display.
LEADING_ZERO_MIN_POINT = 2


def is_auto_range(flags: list[str], range_text: str | None) -> bool:
    return any(flag.strip().casefold() == AUTO_WORD for flag in flags) or (
        (range_text or "").strip().casefold() == AUTO_WORD
    )


def impossible_leading_zero(digits: str, point: int | None) -> bool:
    """An auto-ranging meter does not show a leading zero with the point after the second digit or later ("05.10"):
    it goes to the lower range. "0.123" is possible."""
    return len(digits) > 1 and digits[0] == "0" and (point is None or point >= LEADING_ZERO_MIN_POINT)


# The display profile (meter_counts) is for volts and amperes only, as the bench limit: frequency and capacitance
# often have other counts (for example 9999).
PROFILE_FAMILIES = frozenset({UnitFamily.VOLTAGE, UnitFamily.CURRENT})


def uses_display_profile(unit: str, counts: int) -> bool:
    return counts > 0 and unit_family(unit) in PROFILE_FAMILIES


def display_problems(reading: MultimeterReading, counts: int) -> list[str]:
    """Checks from the display profile (meter_counts; 0: unknown, no check), for volts and amperes only."""
    if not uses_display_profile(reading.unit, counts) or not reading.readable or is_overload(reading.display_text):
        return []
    digits, point = signature(reading.display_text)
    if not digits:
        return []
    problems = []
    expected = display_digits(counts)
    # The lowest range of a function blanks the leading digit (" 12.3" mV on the 600.0 mV range): fewer digits are
    # possible with a prefix (mV, uA, mA), not for the base unit (V, A). More digits are never possible.
    _, prefix = unit_parts(reading.unit)
    if len(digits) > expected or (len(digits) < expected and not prefix):
        problems.append(
            f"the meter shows {expected} digits; the model read {len(digits)} ({reading.display_text}): "
            "a digit or the decimal point is probably wrong"
        )
    if int(digits) >= counts:
        problems.append(
            f"{reading.display_text} is above the {counts} counts of the meter (at most {counts - 1} as digits): "
            "a digit is probably wrong"
        )
    if is_auto_range(reading.flags, reading.range) and impossible_leading_zero(digits, point):
        problems.append(
            f"{reading.display_text} has a leading zero, which an auto-ranging meter does not show (it goes to the "
            "lower range): a digit or the decimal point is probably wrong"
        )
    return problems


def lower(status: MeterStatus, new_problems: list[str], problems: list[str]) -> MeterStatus:
    """Add the problems of a check. A check can only lower a status: "confirmed" becomes "uncertain"."""
    problems.extend(new_problems)
    return MeterStatus.UNCERTAIN if new_problems and status is MeterStatus.CONFIRMED else status


# endregion: digits and decimal point


class ModeSource(StrEnum):
    # The mode from the LCD symbols as the model read them.
    LCD = "lcd"
    # The mode that the user confirmed on the physical dial (bench_state), recent enough to use.
    USER = "user"


class FrameReading(BaseModel):
    """One frame of a multi-frame read: its capture and what the model read in it."""

    capture_id: str
    captured_at: AwareDatetime
    display_text: str
    unit: str
    mode: MeterMode
    value: float | None
    confidence: float
    status: MeterStatus


class MeterResult(BaseModel):
    """What multimeter_read returns: the model's reading as evidence, and the checked conclusion."""

    # region: the model's reading (evidence; the exact LCD text is separate from any number)
    readable: bool
    display_text: str
    unit: str
    mode: MeterMode
    range: str | None
    flags: list[str]
    confidence: float
    notes: str
    # The model's digits and the number of digits before the decimal point (None: no point).
    digits: str | None = None
    digits_before_point: int | None = None
    # endregion
    status: MeterStatus
    unit_family: UnitFamily
    overload: bool
    # The measurement in `unit`. Only set when `status` is "confirmed" (and not for an overload).
    value: float | None
    # The mode that the caller's current test expects. Context only: it never confirms the LCD mode.
    expected_mode: MeterMode | None
    # The nominal value of the current test in the base unit of the mode (V, A, Ω). Context only: it can lower a
    # result, never confirm it.
    expected_value: float | None = None
    problems: list[str]
    # What to do before a numeric conclusion; null when the result is confirmed.
    request: str | None
    # The id (UUID v7) and UTC time of the exact image that the model read (multimeter_read sets them).
    capture_id: str | None = None
    captured_at: AwareDatetime | None = None
    # With a user-confirmed mode: the mode that the model read (the checked `mode` is the user's).
    model_mode: MeterMode | None = None
    mode_source: ModeSource = ModeSource.LCD
    # multimeter_read with several frames: each frame, whether they agree, and the range of the values.
    frames: list[FrameReading] = []
    stable: bool | None = None
    value_min: float | None = None
    value_max: float | None = None


def check_reading(
    reading: MultimeterReading,
    expected_mode: MeterMode | None = None,
    meter_model: str = "",
    user_mode: MeterMode | None = None,
    counts: int = 0,
) -> MeterResult:
    """Check the unit against the mode (and the expected mode). A conflict gives no numeric value.

    `counts` is the display profile of the meter (meter_counts, 0: unknown): the digit count and the highest value.

    `user_mode` is a recent mode that the user confirmed on the dial: it replaces the model's mode for the check (the
    model can read "diode" while the dial is on DC V). A unit of another family still disputes it.
    """
    mode = user_mode or reading.mode
    family = unit_family(reading.unit)
    overload = is_overload(reading.display_text)
    problems: list[str] = []
    status = MeterStatus.CONFIRMED
    if not reading.readable:
        status = MeterStatus.UNREADABLE
        problems.append("the display is not readable")
    else:
        allowed = MODE_FAMILIES[mode]
        if family is UnitFamily.UNKNOWN:
            status = MeterStatus.UNCERTAIN
            problems.append(f"the unit symbol is unknown or not readable (model: {reading.unit!r})")
        elif mode is MeterMode.OTHER:
            status = MeterStatus.UNCERTAIN
            problems.append("the model could not tell the mode")
        elif family not in allowed:
            status = MeterStatus.DISPUTED
            source = "the mode that the user confirmed" if user_mode else "the mode"
            problems.append(f"the unit {reading.unit!r} ({family}) does not fit {source} {mode}")
        if expected_mode is not None and expected_mode is not mode:
            expected = MODE_FAMILIES[expected_mode]
            if family is not UnitFamily.UNKNOWN and family not in expected:
                status = MeterStatus.DISPUTED
                problems.append(
                    f"the current test expects {expected_mode}, but the LCD shows {reading.unit!r} ({family}): "
                    "check the dial and the probes before any conclusion"
                )
            else:
                problems.append(f"the current test expects {expected_mode}; the mode is {mode}")
                if status is MeterStatus.CONFIRMED:
                    status = MeterStatus.UNCERTAIN
        status = lower(status, consistency_problems(reading) + display_problems(reading, counts), problems)
        if status is MeterStatus.CONFIRMED and reading.confidence < MIN_CONFIRMED_CONFIDENCE:
            status = MeterStatus.UNCERTAIN
            problems.append(f"the model confidence {reading.confidence:.2f} is below {MIN_CONFIRMED_CONFIDENCE}")
    confirmed = status is MeterStatus.CONFIRMED
    hint = None if confirmed else dim_hint(reading, meter_model)
    if hint is not None:
        problems.append("the LCD is dim or faint")
    return MeterResult(
        readable=reading.readable,
        display_text=reading.display_text,
        unit=reading.unit,
        mode=mode,
        range=reading.range,
        flags=reading.flags,
        confidence=reading.confidence,
        notes=reading.notes,
        digits=reading.digits,
        digits_before_point=reading.digits_before_point,
        status=status,
        unit_family=family,
        overload=overload,
        value=reading.value if confirmed and not overload else None,
        expected_mode=expected_mode,
        problems=problems,
        request=None if confirmed else " ".join(filter(None, [hint, NEW_FRAME_REQUEST])),
        model_mode=reading.mode if user_mode is not None else None,
        mode_source=ModeSource.USER if user_mode is not None else ModeSource.LCD,
    )


# endregion: check

# region: openrouter wire models


class TextPart(BaseModel):
    type: Literal["text"] = "text"
    text: str


class ImageDetail(StrEnum):
    AUTO = "auto"
    LOW = "low"
    HIGH = "high"


class ImageUrl(BaseModel):
    url: str
    # High detail: the unit symbol on the LCD is only a few pixels high.
    detail: ImageDetail = ImageDetail.HIGH


class ImagePart(BaseModel):
    type: Literal["image_url"] = "image_url"
    image_url: ImageUrl


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str | list[TextPart | ImagePart]


class JsonSchemaFormat(BaseModel):
    name: str
    strict: bool
    schema_: dict[str, Any] = Field(serialization_alias="schema")


class ResponseFormat(BaseModel):
    type: Literal["json_schema"] = "json_schema"
    json_schema: JsonSchemaFormat


class ProviderPreferences(BaseModel):
    require_parameters: bool = True


class Reasoning(BaseModel):
    effort: str = openrouter.REASONING_EFFORT


class ChatCompletionRequest(BaseModel):
    """No `temperature`, `top_p`, or `stop`: `openai/gpt-6-luna` does not accept them (docs/research.md)."""

    model: str
    messages: list[ChatMessage]
    response_format: ResponseFormat
    provider: ProviderPreferences = ProviderPreferences()
    reasoning: Reasoning = Reasoning()
    max_tokens: int


class ResponseMessage(BaseModel):
    content: str | None = None


class Choice(BaseModel):
    message: ResponseMessage
    finish_reason: str | None = None


class ChatCompletionResponse(BaseModel):
    choices: list[Choice]


# endregion: openrouter wire models

# region: client


class VisionError(Exception):
    """The vision request failed or the model did not return a valid reading."""


class MissingApiKeyError(VisionError):
    """OPENROUTER_API_KEY is not set."""


class OutputTruncatedError(VisionError):
    """The model used the full token budget (`finish_reason: length`) before it finished the answer."""


def jpeg_data_url(jpeg: bytes) -> str:
    return openrouter.JPEG_DATA_URL_PREFIX + base64.b64encode(jpeg).decode()


def build_request(model: str, jpeg: bytes, meter_model: str = "", counts: int = 0) -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model=model,
        messages=[
            ChatMessage(role="system", content=SYSTEM_PROMPT),
            ChatMessage(
                role="user",
                content=[
                    TextPart(text=user_prompt(meter_model, counts)),
                    ImagePart(image_url=ImageUrl(url=jpeg_data_url(jpeg))),
                ],
            ),
        ],
        response_format=ResponseFormat(
            json_schema=JsonSchemaFormat(name=openrouter.SCHEMA_NAME, strict=True, schema_=reading_json_schema())
        ),
        max_tokens=openrouter.MAX_TOKENS,
    )


def parse_reading(content: str) -> MultimeterReading:
    """Parse the model answer. Tolerate a Markdown code fence around the JSON."""
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    return MultimeterReading.model_validate_json(text)


class VisionClient:
    def __init__(
        self,
        api_key: SecretStr | None,
        model: str,
        base_url: str,
        timeout: timedelta,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._meter_model = ""
        # The display counts of the meter (0: unknown); the prompt names the digit count.
        self.meter_counts = 0
        self._http = httpx.AsyncClient(base_url=base_url, timeout=timeout.total_seconds(), transport=transport)

    @property
    def model(self) -> str:
        return self._model

    @model.setter
    def model(self, model: str) -> None:
        """The monitor page changes the model at run time."""
        self._model = model

    @property
    def meter_model(self) -> str:
        return self._meter_model

    @meter_model.setter
    def meter_model(self, meter_model: str) -> None:
        """Free text such as "PROSTER T21D". The prompt names it. Empty: no hint."""
        self._meter_model = meter_model

    async def aclose(self) -> None:
        await self._http.aclose()

    def require_api_key(self) -> SecretStr:
        """Raise `MissingApiKeyError` before any other work (for example before the webcam opens)."""
        if self._api_key is None or not self._api_key.get_secret_value():
            raise MissingApiKeyError(
                "OPENROUTER_API_KEY is not set. Put it in the .env file at the repo root or in the environment."
            )
        return self._api_key

    async def read_multimeter(self, jpeg: bytes) -> MultimeterReading:
        api_key = self.require_api_key()
        request = build_request(self._model, jpeg, self._meter_model, self.meter_counts)
        last_error: ValidationError | None = None
        for _ in range(openrouter.MAX_ATTEMPTS):
            content = await self._complete(api_key, request)
            try:
                return parse_reading(content)
            except ValidationError as exc:
                last_error = exc
        raise VisionError(
            f"model {self._model} did not return a valid reading after {openrouter.MAX_ATTEMPTS} attempts: {last_error}"
        )

    async def _complete(self, api_key: SecretStr, request: ChatCompletionRequest) -> str:
        headers = {
            openrouter.AUTHORIZATION_HEADER: openrouter.BEARER_PREFIX + api_key.get_secret_value(),
            CONTENT_TYPE_HEADER: JSON_CONTENT_TYPE,
            openrouter.TITLE_HEADER: openrouter.TITLE,
        }
        try:
            response = await self._http.post(
                openrouter.CHAT_COMPLETIONS_PATH,
                content=request.model_dump_json(by_alias=True, exclude_none=True),
                headers=headers,
            )
        except httpx.TransportError as exc:
            raise VisionError(f"OpenRouter request failed: {exc!r}") from exc
        # OpenRouter sends keep-alive white space before the JSON body.
        preview = response.text.strip()[:ERROR_BODY_PREVIEW_CHARS]
        if response.is_error:
            raise VisionError(f"OpenRouter returned {response.status_code}: {preview}")
        try:
            completion = ChatCompletionResponse.model_validate_json(response.content)
        except ValidationError as exc:
            raise VisionError(f"OpenRouter returned an unexpected body: {preview}") from exc
        if not completion.choices:
            raise VisionError(f"OpenRouter returned no choices: {preview}")
        choice = completion.choices[0]
        if choice.finish_reason == openrouter.FINISH_REASON_LENGTH:
            # A retry with the same budget fails the same way, so do not retry.
            raise OutputTruncatedError(
                f"model {request.model} stopped at the token limit (max_tokens={request.max_tokens}, "
                f"reasoning effort {request.reasoning.effort}) before it finished the JSON answer"
            )
        if not choice.message.content:
            raise VisionError(
                f"OpenRouter returned no message content (finish_reason={choice.finish_reason}): {preview}"
            )
        return choice.message.content


# endregion: client
