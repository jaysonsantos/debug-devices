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
        const val ROTATION = "$PREFIX/rotation"
    }

    object Zoom {
        /** `step: "in"` multiplies the zoom ratio by this factor, `step: "out"` divides it. */
        const val STEP_FACTOR = 1.5f
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
