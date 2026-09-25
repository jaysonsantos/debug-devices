package dev.jayson.debugdevices.camera

import io.ktor.http.HttpStatusCode
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json

// Explicit nulls: the contract writes `"focus": null` and `"distance_diopters": null`. Requests give their
// optional fields a null default, so a missing field still decodes.
val ApiJson = Json {
    encodeDefaults = true
}

@Serializable
data class HealthResponse(val ok: Boolean, @SerialName("app_version") val appVersion: String)

@Serializable
data class CameraStatus(
    @SerialName("zoom_ratio") val zoomRatio: Float,
    @SerialName("min_zoom_ratio") val minZoomRatio: Float,
    @SerialName("max_zoom_ratio") val maxZoomRatio: Float,
    @SerialName("torch_enabled") val torchEnabled: Boolean,
    @SerialName("has_flash_unit") val hasFlashUnit: Boolean,
    @SerialName("rotation_degrees") val rotationDegrees: Int,
    @SerialName("rotation_locked") val rotationLocked: Boolean,
    @SerialName("preview_flip_horizontal") val previewFlipHorizontal: Boolean,
    @SerialName("preview_flip_vertical") val previewFlipVertical: Boolean,
    val focus: FocusInfo?,
    val optics: Optics,
    @SerialName("in_sensor_zoom") val inSensorZoom: InSensorZoomState,
    @SerialName("overlay_boxes") val overlayBoxes: Int
)

@Serializable
enum class ZoomStep {
    @SerialName("in")
    IN,

    @SerialName("out")
    OUT
}

@Serializable
data class ZoomRequest(
    @Serializable(with = StrictFloatSerializer::class) val ratio: Float? = null,
    val step: ZoomStep? = null
)

@Serializable
data class RotationRequest(
    @Serializable(with = StrictIntSerializer::class) val degrees: Int? = null,
    @Serializable(with = StrictBooleanSerializer::class) val auto: Boolean? = null
)

/** The field is required. `ApiJson` refuses unknown fields. */
@Serializable
data class CameraSettingsRequest(
    @SerialName("in_sensor_zoom")
    @Serializable(with = StrictBooleanSerializer::class)
    val inSensorZoom: Boolean
)

/** Both fields are required. `ApiJson` refuses unknown fields. */
@Serializable
data class PreviewRequest(
    @SerialName("flip_horizontal")
    @Serializable(with = StrictBooleanSerializer::class)
    val flipHorizontal: Boolean,
    @SerialName("flip_vertical")
    @Serializable(with = StrictBooleanSerializer::class)
    val flipVertical: Boolean
)

@Serializable
data class TorchRequest(@Serializable(with = StrictBooleanSerializer::class) val enabled: Boolean)

@Serializable
enum class ErrorCode(val status: HttpStatusCode) {
    @SerialName("camera_not_ready")
    CAMERA_NOT_READY(HttpStatusCode.ServiceUnavailable),

    @SerialName("no_flash_unit")
    NO_FLASH_UNIT(HttpStatusCode.Conflict),

    @SerialName("bad_request")
    BAD_REQUEST(HttpStatusCode.BadRequest),

    @SerialName("not_found")
    NOT_FOUND(HttpStatusCode.NotFound),

    @SerialName("method_not_allowed")
    METHOD_NOT_ALLOWED(HttpStatusCode.MethodNotAllowed),

    @SerialName("capture_failed")
    CAPTURE_FAILED(HttpStatusCode.InternalServerError),

    @SerialName("internal_error")
    INTERNAL_ERROR(HttpStatusCode.InternalServerError)
}

@Serializable
data class ApiError(val error: ErrorCode, val message: String)

class ApiException(val code: ErrorCode, override val message: String, cause: Throwable? = null) :
    Exception(message, cause)
