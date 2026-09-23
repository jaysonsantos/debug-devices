package dev.jayson.debugdevices.camera

import android.view.Surface
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class RotationStateTest {
    private val changes = mutableListOf<Int>()
    private val state = RotationState { changes += it }

    @Test
    fun `auto and portrait after start`() {
        assertEquals(Surface.ROTATION_0, state.effectiveRotation)
        assertEquals(0, state.effectiveDegrees)
        assertFalse(state.isLocked)
    }

    @Test
    fun `sensor changes the rotation in auto`() {
        state.onSensorAngle(90)
        assertEquals(Surface.ROTATION_270, state.effectiveRotation)
        assertEquals(270, state.effectiveDegrees)
        assertEquals(listOf(Surface.ROTATION_270), changes)
    }

    @Test
    fun `lock wins over the sensor, and auto goes back to the current sensor value`() {
        state.lock(Surface.ROTATION_90)
        assertTrue(state.isLocked)
        assertEquals(90, state.effectiveDegrees)
        state.onSensorAngle(180)
        assertEquals(Surface.ROTATION_90, state.effectiveRotation)
        state.lock(null)
        assertFalse(state.isLocked)
        assertEquals(Surface.ROTATION_180, state.effectiveRotation)
        assertEquals(listOf(Surface.ROTATION_90, Surface.ROTATION_180), changes)
    }

    @Test
    fun `no callback when the effective rotation stays the same`() {
        state.lock(Surface.ROTATION_0)
        state.onSensorAngle(Constants.Orientation.UNKNOWN_ANGLE)
        state.lock(null)
        assertEquals(emptyList<Int>(), changes)
    }
}
