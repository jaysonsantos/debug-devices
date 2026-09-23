package dev.jayson.debugdevices.camera

import android.view.Surface
import dev.jayson.debugdevices.camera.Constants.Orientation.BUCKET_DEGREES
import dev.jayson.debugdevices.camera.Constants.Orientation.DEGREES_PER_TURN
import dev.jayson.debugdevices.camera.Constants.Orientation.SWITCH_DISTANCE_DEGREES
import dev.jayson.debugdevices.camera.Constants.Orientation.UNKNOWN_ANGLE
import kotlin.math.abs

/**
 * Maps the physical device angle from `OrientationEventListener` (0 = natural portrait, clockwise) to a
 * `Surface.ROTATION_*` value for `ImageCapture.targetRotation`. Four buckets with hysteresis: the rotation changes
 * only when the angle is [SWITCH_DISTANCE_DEGREES] away from the center of the current bucket, so a phone held
 * near 45 degrees does not flip on every sensor event.
 *
 * `Surface.ROTATION_*` are compile-time constants, so JVM unit tests cover this object.
 */
object OrientationLogic {
    /** True when the phone is sideways: an upright overlay then needs width and height swapped. */
    fun isSideways(rotation: Int): Boolean = rotation == Surface.ROTATION_90 || rotation == Surface.ROTATION_270

    /** Device angle bucket centers, in order 0, 90, 180, 270, and the surface rotation of each one. */
    private val surfaceRotationByBucket = intArrayOf(
        Surface.ROTATION_0,
        Surface.ROTATION_270,
        Surface.ROTATION_180,
        Surface.ROTATION_90
    )

    fun nextRotation(angle: Int, current: Int): Int {
        if (angle == UNKNOWN_ANGLE) return current
        val normalized = normalize(angle)
        if (distance(normalized, deviceAngleOf(current)) < SWITCH_DISTANCE_DEGREES) return current
        val bucket = ((normalized + BUCKET_DEGREES / 2) / BUCKET_DEGREES) % surfaceRotationByBucket.size
        return surfaceRotationByBucket[bucket]
    }

    /**
     * Resolves a rotation request: the locked `Surface.ROTATION_*` value, or null for auto.
     * Throws [ApiException] with [ErrorCode.BAD_REQUEST] for both fields, no field, bad degrees, or `auto: false`.
     */
    fun lockedRotationFor(request: RotationRequest): Int? {
        val degrees = request.degrees
        val auto = request.auto
        if ((degrees == null) == (auto == null)) {
            throw ApiException(ErrorCode.BAD_REQUEST, Constants.Messages.ROTATION_NEEDS_ONE_FIELD)
        }
        if (auto != null) {
            if (!auto) throw ApiException(ErrorCode.BAD_REQUEST, Constants.Messages.ROTATION_AUTO_TRUE)
            return null
        }
        val valid = degrees != null && degrees in 0 until DEGREES_PER_TURN && degrees % BUCKET_DEGREES == 0
        if (!valid) throw ApiException(ErrorCode.BAD_REQUEST, Constants.Messages.ROTATION_BAD_DEGREES)
        return degrees / BUCKET_DEGREES
    }

    /**
     * Degrees of a `Surface.ROTATION_*` value: 0, 90, 180, 270 (the constants are 0 to 3). A view rotated by this
     * reads upright.
     */
    fun surfaceDegrees(rotation: Int): Int = rotation * BUCKET_DEGREES

    /** Device angle bucket center of a surface rotation. The two turn in opposite directions. */
    private fun deviceAngleOf(rotation: Int): Int = normalize(-surfaceDegrees(rotation))

    private fun normalize(angle: Int): Int = ((angle % DEGREES_PER_TURN) + DEGREES_PER_TURN) % DEGREES_PER_TURN

    private fun distance(a: Int, b: Int): Int {
        val diff = abs(a - b)
        return minOf(diff, DEGREES_PER_TURN - diff)
    }
}
