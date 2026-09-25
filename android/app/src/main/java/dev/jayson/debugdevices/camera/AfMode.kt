package dev.jayson.debugdevices.camera

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/** Autofocus mode of `POST /v1/camera`. `continuous` after an app start. */
@Serializable
enum class AfMode {
    @SerialName("continuous")
    CONTINUOUS,

    /** The camera's close-range autofocus (`CONTROL_AF_MODE_MACRO`). The lens moves on a focus trigger only. */
    @SerialName("macro")
    MACRO
}

/** Pure rules of the camera settings, so JVM unit tests cover them. */
object CameraSettingsLogic {
    /** Throws [ApiException] with [ErrorCode.BAD_REQUEST] when the body sets nothing. */
    fun validate(request: CameraSettingsRequest) {
        if (request.inSensorZoom == null && request.afMode == null) {
            throw ApiException(ErrorCode.BAD_REQUEST, Constants.Messages.CAMERA_SETTINGS_EMPTY)
        }
    }

    /** A phone without the macro mode stays in continuous autofocus (the request still succeeds). */
    fun effectiveAfMode(requested: AfMode, macroSupported: Boolean): AfMode =
        if (requested == AfMode.MACRO && !macroSupported) AfMode.CONTINUOUS else requested
}
