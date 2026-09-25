package dev.jayson.debugdevices.camera

/** State of the Qualcomm vendor in-sensor zoom experiment (see `docs/research/phone-lenses.md`, option B). */
enum class InSensorZoomState {
    /** Not requested. The default. */
    OFF,

    /** Requested, and the session runs with the vendor session parameter. */
    ON,

    /** Requested, but the camera does not publish the vendor key. Bound without it. */
    UNSUPPORTED,

    /** Requested and the key exists, but the session failed or showed no preview frames. Bound again without it. */
    FALLBACK
}

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

    fun plan(requested: Boolean, presence: VendorKeyPresence): InSensorZoomState = when {
        !requested -> InSensorZoomState.OFF
        !presence.isAvailable -> InSensorZoomState.UNSUPPORTED
        else -> InSensorZoomState.ON
    }

    /** A session is stable when the preview streamed and the camera reported no error in the check window. */
    fun isStable(events: Collection<SessionEvent>): Boolean =
        SessionEvent.PREVIEW_STREAMING in events && SessionEvent.CAMERA_ERROR !in events

    /** An [InSensorZoomState.ON] session that is not stable falls back. Other states stay. */
    fun afterSessionCheck(state: InSensorZoomState, stable: Boolean): InSensorZoomState =
        if (state == InSensorZoomState.ON && !stable) InSensorZoomState.FALLBACK else state

    /** Only an [InSensorZoomState.ON] session sets the vendor parameter. */
    fun setsVendorParameter(state: InSensorZoomState): Boolean = state == InSensorZoomState.ON
}
