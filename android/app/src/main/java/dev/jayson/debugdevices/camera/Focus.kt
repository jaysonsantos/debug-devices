package dev.jayson.debugdevices.camera

import android.hardware.camera2.CameraMetadata
import kotlin.math.roundToInt
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

@Serializable
enum class FocusState {
    @SerialName("focused")
    FOCUSED,

    @SerialName("scanning")
    SCANNING,

    @SerialName("unfocused")
    UNFOCUSED,

    @SerialName("unknown")
    UNKNOWN
}

@Serializable
enum class FocusCalibration {
    @SerialName("uncalibrated")
    UNCALIBRATED,

    @SerialName("approximate")
    APPROXIMATE,

    @SerialName("calibrated")
    CALIBRATED
}

@Serializable
data class FocusInfo(
    @SerialName("distance_diopters") val distanceDiopters: Float?,
    val state: FocusState,
    val calibration: FocusCalibration,
    @SerialName("min_distance_diopters") val minDistanceDiopters: Float
)

@Serializable
data class Optics(
    @SerialName("focal_length_mm") val focalLengthMm: Float,
    @SerialName("sensor_width_mm") val sensorWidthMm: Float,
    @SerialName("output_width_px") val outputWidthPx: Int
)

/** The latest preview capture result values. Small and immutable, replaced only when a value changes. */
data class FocusSample(val distanceDiopters: Float?, val afState: Int?)

/** Static focus data of the bound camera. */
data class FocusStatic(val calibration: Int?, val minDistanceDiopters: Float?)

/** Pure focus rules, so JVM unit tests cover them. */
object FocusLogic {
    fun state(afState: Int?): FocusState = when (afState) {
        CameraMetadata.CONTROL_AF_STATE_FOCUSED_LOCKED,
        CameraMetadata.CONTROL_AF_STATE_PASSIVE_FOCUSED -> FocusState.FOCUSED

        CameraMetadata.CONTROL_AF_STATE_ACTIVE_SCAN,
        CameraMetadata.CONTROL_AF_STATE_PASSIVE_SCAN -> FocusState.SCANNING

        CameraMetadata.CONTROL_AF_STATE_NOT_FOCUSED_LOCKED,
        CameraMetadata.CONTROL_AF_STATE_PASSIVE_UNFOCUSED -> FocusState.UNFOCUSED

        else -> FocusState.UNKNOWN
    }

    /** A missing value is the safe answer: not in real units. */
    fun calibration(value: Int?): FocusCalibration = when (value) {
        CameraMetadata.LENS_INFO_FOCUS_DISTANCE_CALIBRATION_APPROXIMATE -> FocusCalibration.APPROXIMATE
        CameraMetadata.LENS_INFO_FOCUS_DISTANCE_CALIBRATION_CALIBRATED -> FocusCalibration.CALIBRATED
        else -> FocusCalibration.UNCALIBRATED
    }

    /** Null before the camera is bound. The distance is null until the first result, and always with fixed focus. */
    fun info(static: FocusStatic?, sample: FocusSample?): FocusInfo? {
        if (static == null) return null
        val minDistance = static.minDistanceDiopters ?: Constants.Focus.FIXED_FOCUS_DIOPTERS
        val fixedFocus = minDistance == Constants.Focus.FIXED_FOCUS_DIOPTERS
        return FocusInfo(
            distanceDiopters = if (fixedFocus) null else sample?.distanceDiopters,
            state = state(sample?.afState),
            calibration = calibration(static.calibration),
            minDistanceDiopters = minDistance
        )
    }

    /** Distance in whole cm for the label, or null when it has no real unit, is unknown, or is infinity. */
    fun distanceCm(info: FocusInfo?): Int? {
        val diopters = info?.distanceDiopters ?: return null
        if (info.calibration == FocusCalibration.UNCALIBRATED || diopters <= 0f) return null
        return (Constants.Focus.CM_PER_METER / diopters).roundToInt()
    }

    /** The output width before rotation: the long side, because the sensor output is landscape. */
    fun outputWidthPx(width: Int, height: Int): Int = maxOf(width, height)
}
