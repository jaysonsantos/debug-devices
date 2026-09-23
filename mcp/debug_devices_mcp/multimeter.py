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
    "You read a digital multimeter from one photo. Report only what the photo shows. "
    "Read every digit, the decimal point, the sign, and the unit prefix on the LCD. "
    "Find the mode from the rotary dial position and the display symbols (DC bar or '⎓', AC '~', Ω, the diode "
    "symbol, the buzzer symbol for continuity, F, Hz, %, °C). Put 'OL' in display_text and null in value for an "
    "overload. If you cannot read the display, set readable to false and explain why in notes. "
    "Answer with one JSON object that matches the schema. No other text."
)
USER_PROMPT = "Read this multimeter."


DEFS_KEY = "$defs"
REF_KEY = "$ref"
REF_PREFIX = "#/$defs/"


def _inline_refs(node: Any, defs: dict[str, Any]) -> Any:
    """Replace each `$ref` with a copy of its definition. Keep the keywords next to the `$ref`."""
    if isinstance(node, list):
        return [_inline_refs(item, defs) for item in node]
    if not isinstance(node, dict):
        return node
    if REF_KEY in node:
        siblings = {key: value for key, value in node.items() if key != REF_KEY}
        target = defs[node[REF_KEY].removeprefix(REF_PREFIX)]
        return {**_inline_refs(target, defs), **_inline_refs(siblings, defs)}
    return {key: _inline_refs(value, defs) for key, value in node.items()}


def reading_json_schema() -> dict[str, Any]:
    """JSON schema for strict structured outputs. OpenAI strict mode refuses keywords next to a `$ref`."""
    schema = MultimeterReading.model_json_schema()
    defs = schema.pop(DEFS_KEY, {})
    return _inline_refs(schema, defs)


# endregion: reading

# region: openrouter wire models


class TextPart(BaseModel):
    type: Literal["text"] = "text"
    text: str


class ImageUrl(BaseModel):
    url: str


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


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    response_format: ResponseFormat
    temperature: float


class ResponseMessage(BaseModel):
    content: str | None = None


class Choice(BaseModel):
    message: ResponseMessage


class ChatCompletionResponse(BaseModel):
    choices: list[Choice]


# endregion: openrouter wire models

# region: client


class VisionError(Exception):
    """The vision request failed or the model did not return a valid reading."""


class MissingApiKeyError(VisionError):
    """OPENROUTER_API_KEY is not set."""


def jpeg_data_url(jpeg: bytes) -> str:
    return openrouter.JPEG_DATA_URL_PREFIX + base64.b64encode(jpeg).decode()


def build_request(model: str, jpeg: bytes) -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model=model,
        messages=[
            ChatMessage(role="system", content=SYSTEM_PROMPT),
            ChatMessage(
                role="user",
                content=[TextPart(text=USER_PROMPT), ImagePart(image_url=ImageUrl(url=jpeg_data_url(jpeg)))],
            ),
        ],
        response_format=ResponseFormat(
            json_schema=JsonSchemaFormat(name=openrouter.SCHEMA_NAME, strict=True, schema_=reading_json_schema())
        ),
        temperature=openrouter.TEMPERATURE,
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
        self._http = httpx.AsyncClient(base_url=base_url, timeout=timeout.total_seconds(), transport=transport)

    @property
    def model(self) -> str:
        return self._model

    async def aclose(self) -> None:
        await self._http.aclose()

    async def read_multimeter(self, jpeg: bytes) -> MultimeterReading:
        if self._api_key is None or not self._api_key.get_secret_value():
            raise MissingApiKeyError(
                "OPENROUTER_API_KEY is not set. Put it in the .env file at the repo root or in the environment."
            )
        request = build_request(self._model, jpeg)
        last_error: ValidationError | None = None
        for _ in range(openrouter.MAX_ATTEMPTS):
            content = await self._complete(self._api_key, request)
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
        preview = response.text[:ERROR_BODY_PREVIEW_CHARS]
        if response.is_error:
            raise VisionError(f"OpenRouter returned {response.status_code}: {preview}")
        try:
            completion = ChatCompletionResponse.model_validate_json(response.content)
        except ValidationError as exc:
            raise VisionError(f"OpenRouter returned an unexpected body: {preview}") from exc
        if not completion.choices or not completion.choices[0].message.content:
            raise VisionError(f"OpenRouter returned no message content: {preview}")
        return completion.choices[0].message.content


# endregion: client
