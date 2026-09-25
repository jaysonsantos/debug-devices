package dev.jayson.debugdevices.camera

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/** State of the vendor in-sensor zoom. The API words come from `docs/phone-api.md`. */
@Serializable
enum class InSensorZoomState {
    /** Not requested. The default. */
    @SerialName("off")
    OFF,

    /** Requested: the session runs with the vendor session type 0x9005 and the vendor session parameter. */
    @SerialName("on")
    ON,

    /** Requested, but the camera does not publish the vendor key, or Android is older than 9. Bound without it. */
    @SerialName("unsupported")
    UNSUPPORTED,

    /** Requested, but the vendor session failed to bind or showed no preview frames. Bound again in NORMAL mode. */
    @SerialName("fallback")
    FALLBACK
}

/** Zoom and torch to set again after a rebind. */
data class CameraRestore(val zoomRatio: Float, val torchEnabled: Boolean)

/** Signals from a new camera session. */
enum class SessionEvent {
    PREVIEW_STREAMING,
    CAMERA_ERROR
}

/** Where the camera publishes the vendor key. */
data class VendorKeyPresence(val inSessionKeys: Boolean, val inRequestKeys: Boolean) {
    val isAvailable: Boolean
        get() = inSessionKeys || inRequestKeys
}

/** Pure rules of the experiment, so JVM unit tests cover them. */
object InSensorZoomLogic {
    /** The intent extra wins when it is present. Without it, the current setting stays. */
    fun requested(current: Boolean, extraPresent: Boolean, extraValue: Boolean): Boolean =
        if (extraPresent) extraValue else current

    fun presence(sessionKeyNames: Collection<String>, requestKeyNames: Collection<String>): VendorKeyPresence =
        VendorKeyPresence(
            inSessionKeys = Constants.InSensorZoom.VENDOR_KEY in sessionKeyNames,
            inRequestKeys = Constants.InSensorZoom.VENDOR_KEY in requestKeyNames
        )

    /** [sessionTypeSupported]: Android 9 or newer, where a session type can be set. */
    fun plan(requested: Boolean, presence: VendorKeyPresence, sessionTypeSupported: Boolean): InSensorZoomState = when {
        !requested -> InSensorZoomState.OFF
        !presence.isAvailable || !sessionTypeSupported -> InSensorZoomState.UNSUPPORTED
        else -> InSensorZoomState.ON
    }

    /** The vendor session type for [InSensorZoomState.ON], else null (NORMAL mode). */
    fun sessionType(state: InSensorZoomState): Int? =
        if (state == InSensorZoomState.ON) Constants.InSensorZoom.VENDOR_SESSION_TYPE else null

    /** A bind that throws with the vendor session falls back. Other states have nothing to fall back to. */
    fun afterBindFailure(state: InSensorZoomState): InSensorZoomState =
        if (state == InSensorZoomState.ON) InSensorZoomState.FALLBACK else state

    /** A session is stable when the preview streamed and the camera reported no error in the check window. */
    fun isStable(events: Collection<SessionEvent>): Boolean =
        SessionEvent.PREVIEW_STREAMING in events && SessionEvent.CAMERA_ERROR !in events

    /** An [InSensorZoomState.ON] session that is not stable falls back. Other states stay. */
    fun afterSessionCheck(state: InSensorZoomState, stable: Boolean): InSensorZoomState =
        if (state == InSensorZoomState.ON && !stable) InSensorZoomState.FALLBACK else state

    /**
     * Vendor int32 parameters for an [InSensorZoomState.ON] session, set as session parameters and as request
     * options. `xiaomi.app.module = 163` is what the Xiaomi camera app sends in photo mode; without it, the HAL
     * keeps `InSensorZoomState = 0` in the vendor session (seen on 7fad170e). Empty for the other states.
     */
    fun vendorParameters(state: InSensorZoomState): Map<String, Int> = if (state == InSensorZoomState.ON) {
        mapOf(
            Constants.InSensorZoom.VENDOR_KEY to Constants.InSensorZoom.ENABLED_VALUE,
            Constants.InSensorZoom.APP_MODULE_KEY to Constants.InSensorZoom.APP_MODULE_PHOTO
        )
    } else {
        emptyMap()
    }

    /**
     * The zoom steps from [from] to [to]. In an [InSensorZoomState.ON] session, a jump from below the half-field
     * ratio to the quarter-field ratio or more goes through the entry ratio, so the HAL enters the half-field mode.
     */
    fun zoomPath(state: InSensorZoomState, from: Float, to: Float): List<Float> {
        val jumpsOverEntry = from < Constants.InSensorZoom.HALF_FIELD_RATIO &&
            to >= Constants.InSensorZoom.QUARTER_FIELD_RATIO
        return if (state == InSensorZoomState.ON && jumpsOverEntry) {
            listOf(Constants.InSensorZoom.ENTRY_RATIO, to)
        } else {
            listOf(to)
        }
    }

    /** A new value binds again. `true` again after a fallback tries the vendor session once more. */
    fun needsReconfigure(requested: Boolean, state: InSensorZoomState, enabled: Boolean): Boolean =
        enabled != requested || (enabled && state == InSensorZoomState.FALLBACK)

    /** Only an [InSensorZoomState.ON] session sets the vendor parameter. */
    fun setsVendorParameter(state: InSensorZoomState): Boolean = state == InSensorZoomState.ON
}
