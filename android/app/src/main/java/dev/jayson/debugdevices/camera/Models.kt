package dev.jayson.debugdevices.camera

import io.ktor.http.HttpStatusCode
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json

val ApiJson = Json {
    explicitNulls = false
    encodeDefaults = true
}

@Serializable
data class HealthResponse(
    val ok: Boolean,
    @SerialName("app_version") val appVersion: String,
)

@Serializable
data class CameraStatus(
    @SerialName("zoom_ratio") val zoomRatio: Float,
    @SerialName("min_zoom_ratio") val minZoomRatio: Float,
    @SerialName("max_zoom_ratio") val maxZoomRatio: Float,
    @SerialName("torch_enabled") val torchEnabled: Boolean,
    @SerialName("has_flash_unit") val hasFlashUnit: Boolean,
)

@Serializable
enum class ZoomStep {
    @SerialName("in")
    IN,

    @SerialName("out")
    OUT,
}

@Serializable
data class ZoomRequest(
    val ratio: Float? = null,
    val step: ZoomStep? = null,
)

@Serializable
data class TorchRequest(
    val enabled: Boolean,
)

@Serializable
enum class ErrorCode(
    val status: HttpStatusCode,
) {
    @SerialName("camera_not_ready")
    CAMERA_NOT_READY(HttpStatusCode.ServiceUnavailable),

    @SerialName("no_flash_unit")
    NO_FLASH_UNIT(HttpStatusCode.Conflict),

    @SerialName("bad_request")
    BAD_REQUEST(HttpStatusCode.BadRequest),

    @SerialName("not_found")
    NOT_FOUND(HttpStatusCode.NotFound),

    @SerialName("capture_failed")
    CAPTURE_FAILED(HttpStatusCode.InternalServerError),
}

@Serializable
data class ApiError(
    val error: ErrorCode,
    val message: String,
)

class ApiException(
    val code: ErrorCode,
    override val message: String,
    cause: Throwable? = null,
) : Exception(message, cause)
