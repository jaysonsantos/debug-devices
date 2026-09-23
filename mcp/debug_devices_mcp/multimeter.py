"""Read a multimeter display from a webcam frame with an OpenRouter vision model."""

import base64
from datetime import timedelta
from enum import StrEnum
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

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
    value: float | None = Field(
        description="Numeric value in `unit`, with the sign. Null when not readable or when the display shows OL."
    )
    unit: str = Field(description='Unit as shown, with its prefix, e.g. "mV", "V", "kΩ", "µF", "°C", "Hz", "%".')
    display_text: str = Field(description="Exact digits and symbols on the display, e.g. '-0.123' or 'OL'.")
    mode: MeterMode = Field(description="Measurement mode from the dial position and the display symbols.")
    range: str | None = Field(description='Selected range, e.g. "20V", or "auto". Null when unknown.')
    flags: list[str] = Field(description='Indicators on the display, e.g. "HOLD", "AUTO", "REL", "low battery".')
    confidence: float = Field(ge=0, le=1, description="Confidence in the value, from 0 to 1.")
    notes: str = Field(description="Short remarks: glare, a blurred digit, a reason for low confidence. Can be empty.")


SYSTEM_PROMPT = (
    "You read a digital multimeter from one photo. Report only what the photo shows.\n"
    "- Read every digit, the decimal point, the sign, and the unit with its prefix on the LCD.\n"
    "- The unit symbol and the annunciators on the LCD decide the mode: V or mV with the DC bar or '⎓' is "
    "dc_voltage, with '~' or AC is ac_voltage, A/mA/µA the same way for current, Ω/kΩ/MΩ is resistance, "
    "the diode symbol is diode, the buzzer symbol is continuity, nF/µF is capacitance, Hz is frequency, "
    "% is duty_cycle, °C/°F is temperature.\n"
    "- The rotary dial only helps. On many meters one dial position has several functions "
    "(for example resistance, diode, and continuity). Do not take the mode from the dial alone.\n"
    "- If you cannot read the unit symbol, say so in notes, give the mode that the dial and the digits suggest, "
    "and set confidence to 0.5 or lower.\n"
    "- For an overload, put 'OL' in display_text and null in value.\n"
    "- If the display is blank or you cannot read the digits, set readable to false and say why in notes.\n"
    "Answer with one JSON object that matches the schema. No other text."
)
USER_PROMPT = "Read this multimeter."
METER_MODEL_PROMPT = "The meter is a {meter_model}. Use what you know about its display and dial."


def user_prompt(meter_model: str) -> str:
    if not meter_model.strip():
        return USER_PROMPT
    return f"{USER_PROMPT} {METER_MODEL_PROMPT.format(meter_model=meter_model.strip())}"


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


def build_request(model: str, jpeg: bytes, meter_model: str = "") -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model=model,
        messages=[
            ChatMessage(role="system", content=SYSTEM_PROMPT),
            ChatMessage(
                role="user",
                content=[
                    TextPart(text=user_prompt(meter_model)),
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
        request = build_request(self._model, jpeg, self._meter_model)
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
