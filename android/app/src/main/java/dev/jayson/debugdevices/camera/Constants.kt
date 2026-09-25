package dev.jayson.debugdevices.camera

/** Every value with a meaning lives here. The contract is `docs/phone-api.md`. */
object Constants {
    object Server {
        const val HOST = "127.0.0.1"
        const val PORT = 8765
        const val STOP_GRACE_PERIOD_MILLIS = 100L
        const val STOP_TIMEOUT_MILLIS = 500L
    }

    object Log {
        const val TAG = "DebugCamera"
    }

    object Paths {
        private const val PREFIX = "/v1"
        const val HEALTH = "$PREFIX/health"
        const val STATUS = "$PREFIX/status"
        const val ZOOM = "$PREFIX/zoom"
        const val TORCH = "$PREFIX/torch"
        const val SNAPSHOT = "$PREFIX/snapshot"
        const val CAMERA = "$PREFIX/camera"
        const val ROTATION = "$PREFIX/rotation"
        const val PREVIEW = "$PREFIX/preview"
        const val FOCUS = "$PREFIX/focus"
        const val OVERLAY = "$PREFIX/overlay"
    }

    object Zoom {
        /** `step: "in"` multiplies the zoom ratio by this factor, `step: "out"` divides it. */
        const val STEP_FACTOR = 1.5f

        /** Zoom ratio 1x: the fallback when the camera has no zoom state yet. */
        const val UNIT_RATIO = 1f
    }

    object Orientation {
        const val DEGREES_PER_TURN = 360
        private const val BUCKETS = 4
        const val BUCKET_DEGREES = DEGREES_PER_TURN / BUCKETS

        /** Extra degrees past the bucket edge before the rotation changes. */
        const val HYSTERESIS_DEGREES = 15
        const val SWITCH_DISTANCE_DEGREES = BUCKET_DEGREES / 2 + HYSTERESIS_DEGREES

        /** Same value as `OrientationEventListener.ORIENTATION_UNKNOWN` (phone flat on a table). */
        const val UNKNOWN_ANGLE = -1
    }

    object InSensorZoom {
        /** Qualcomm vendor session parameter (int32) of the 200 MP main camera. */
        const val VENDOR_KEY = "org.codeaurora.qcamera3.sessionParameters.EnableInsensorZoom"
        const val ENABLED_VALUE = 1

        /**
         * Vendor session operation mode that the Xiaomi camera app uses for real in-sensor zoom
         * (`CUSTOM (36869)` in `dumpsys media.camera`, see `docs/research/xiaomi-app-zoom.md`).
         */
        const val VENDOR_SESSION_TYPE = 0x9005

        /** Xiaomi session key for the camera app mode, and the value of the Xiaomi app's photo mode. */
        const val APP_MODULE_KEY = "xiaomi.app.module"
        const val APP_MODULE_PHOTO = 163

        /** `adb shell am start -n .../.MainActivity --ez in_sensor_zoom true`. Off by default. */
        const val INTENT_EXTRA = "in_sensor_zoom"

        /**
         * The HAL enters the half-field sensor mode only when the zoom lands in [HALF_FIELD_RATIO, QUARTER_FIELD_RATIO)
         * and then keeps it above QUARTER_FIELD_RATIO. A direct jump from below 2x to 4x or more never enters it
         * (seen on 7fad170e). So such a jump goes through ENTRY_RATIO first and waits ENTRY_SETTLE_MILLIS.
         */
        const val HALF_FIELD_RATIO = 2f
        const val QUARTER_FIELD_RATIO = 4f
        const val ENTRY_RATIO = 3f
        const val ENTRY_SETTLE_MILLIS = 300L

        /** How long a new session with the parameter must stream without a camera error. */
        const val SESSION_CHECK_MILLIS = 4_000L
    }

    object Overlay {
        const val MAX_BOXES = 8
        const val MAX_LABEL_LENGTH = 32

        /** Float rounding: a box may end this much past 1.0 (x + width, y + height). */
        const val EDGE_TOLERANCE = 1e-4f

        /** The boxes go away after this time (`OVERLAY_TTL` in the contract). */
        const val TTL_MINUTES = 10L
        const val TTL_MILLIS = TTL_MINUTES * 60L * 1_000L
    }

    object FocusTap {
        /** The focus lock ends after this time, then the camera goes back to continuous autofocus. */
        const val HOLD_SECONDS = 5L
        const val HOLD_MILLIS = HOLD_SECONDS * 1_000L

        /** How long the focus ring stays on the screen after a tap. */
        const val RING_MILLIS = 800L
    }

    object Focus {
        /** `LENS_INFO_MINIMUM_FOCUS_DISTANCE` of a fixed-focus lens. */
        const val FIXED_FOCUS_DIOPTERS = 0f
        const val CM_PER_METER = 100f

        /** Placeholders when the camera does not publish a value. */
        const val UNKNOWN_MM = 0f
        const val UNKNOWN_PX = 0

        /** How often the phone label reads the focus distance again. */
        const val LABEL_REFRESH_MILLIS = 1_000L
    }

    object Start {
        /** After an app start the torch is off. The zoom starts at the minimum ratio (see [ZoomLogic.startRatio]). */
        const val TORCH_ENABLED = false
    }

    object Messages {
        const val CAMERA_NOT_READY = "Camera is not bound yet"
        const val CAMERA_NOT_ACTIVE = "Camera is not active"
        const val NO_FLASH_UNIT = "This camera has no flash unit"
        const val ZOOM_NEEDS_ONE_FIELD = "Send exactly one of 'ratio' or 'step'"
        const val ZOOM_RATIO_NOT_FINITE = "'ratio' must be a finite number"
        const val BAD_BODY = "The request body is not valid JSON for this endpoint"
        const val NOT_FOUND = "No such endpoint"
        const val METHOD_NOT_ALLOWED = "This method is not allowed on this endpoint"
        const val CAPTURE_FAILED = "The still capture failed"
        const val UNEXPECTED = "Unexpected error. The app log has the stack trace"
        const val IN_SENSOR_ZOOM_FALLBACK = "In-sensor zoom session failed, binding again without it"
        const val REBIND_REASON_API = "in_sensor_zoom request"
        const val REBIND_FAILED = "In-sensor zoom rebind failed"
        const val IN_SENSOR_ZOOM_BIND_FAILED = "Vendor session bind failed, binding again in NORMAL mode"
        const val OVERLAY_TOO_MANY = "At most 8 boxes"
        const val OVERLAY_BAD_BOX = "A box must be inside the snapshot, with width and height above 0"
        const val OVERLAY_LABEL_TOO_LONG = "A label has at most 32 characters"
        const val FOCUS_NEEDS_ONE_PAIR = "Send exactly one pair: screen_x and screen_y, or snapshot_x and snapshot_y"
        const val FOCUS_OUT_OF_RANGE = "Focus coordinates must be numbers in [0, 1]"
        const val FOCUS_OUTSIDE_PREVIEW = "outside the preview"
        const val START_STATE_PENDING = "Camera start state is not set yet"
        const val ROTATION_CHANGED = "Snapshot rotation degrees: "
        const val START_STATE_FAILED = "Camera bind or start state failed"
        const val EXPECTED_NUMBER = "Expected a JSON number"
        const val EXPECTED_INTEGER = "Expected a JSON integer"
        const val ROTATION_NEEDS_ONE_FIELD = "Send exactly one of 'degrees' or 'auto'"
        const val ROTATION_BAD_DEGREES = "'degrees' must be 0, 90, 180, or 270"
        const val ROTATION_AUTO_TRUE = "'auto' must be true"
        const val EXPECTED_BOOLEAN = "Expected a JSON boolean"
        const val EXPECTED_LITERAL = "Expected a JSON number or boolean, not a string"
        const val JSON_ONLY = "This serializer reads JSON only"
    }
}
