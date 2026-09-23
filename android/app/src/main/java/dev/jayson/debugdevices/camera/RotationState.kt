package dev.jayson.debugdevices.camera

import android.view.Surface

/**
 * The snapshot rotation: the physical orientation from the sensor, or a rotation that the API locked.
 * The sensor value stays current while locked, so `{"auto": true}` goes back to how the phone is held now.
 * [onChange] gets the new effective `Surface.ROTATION_*` value. Not thread-safe: call it on one thread.
 */
class RotationState(private val onChange: (Int) -> Unit) {
    var sensorRotation = Surface.ROTATION_0
        private set

    /** The locked `Surface.ROTATION_*` value, or null for auto. Auto after an app start. */
    var lockedRotation: Int? = null
        private set

    val effectiveRotation: Int
        get() = lockedRotation ?: sensorRotation

    val isLocked: Boolean
        get() = lockedRotation != null

    val effectiveDegrees: Int
        get() = OrientationLogic.surfaceDegrees(effectiveRotation)

    fun onSensorAngle(angle: Int) = update { sensorRotation = OrientationLogic.nextRotation(angle, sensorRotation) }

    /** Locks to [rotation], or goes back to auto with null. */
    fun lock(rotation: Int?) = update { lockedRotation = rotation }

    private inline fun update(block: () -> Unit) {
        val before = effectiveRotation
        block()
        if (effectiveRotation != before) onChange(effectiveRotation)
    }
}
