package dev.jayson.debugdevices.camera

import android.view.OrientationEventListener
import android.view.Surface
import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Test

class OrientationLogicTest {
    private fun fromPortrait(angle: Int) = OrientationLogic.nextRotation(angle, Surface.ROTATION_0)

    @Test
    fun `bucket centers map to the camerax rotations`() {
        assertEquals(Surface.ROTATION_0, fromPortrait(0))
        assertEquals(Surface.ROTATION_270, fromPortrait(90))
        assertEquals(Surface.ROTATION_180, fromPortrait(180))
        assertEquals(Surface.ROTATION_90, fromPortrait(270))
    }

    @Test
    fun `angles near 360 are portrait`() {
        assertEquals(Surface.ROTATION_0, fromPortrait(359))
        assertEquals(Surface.ROTATION_0, fromPortrait(360))
    }

    @Test
    fun `unknown angle keeps the current rotation`() {
        assertEquals(OrientationEventListener.ORIENTATION_UNKNOWN, Constants.Orientation.UNKNOWN_ANGLE)
        assertEquals(
            Surface.ROTATION_90,
            OrientationLogic.nextRotation(Constants.Orientation.UNKNOWN_ANGLE, Surface.ROTATION_90)
        )
    }

    @Test
    fun `hysteresis keeps portrait until 60 degrees`() {
        assertEquals(Surface.ROTATION_0, fromPortrait(46))
        assertEquals(Surface.ROTATION_0, fromPortrait(59))
        assertEquals(Surface.ROTATION_270, fromPortrait(60))
        assertEquals(Surface.ROTATION_0, fromPortrait(314))
        assertEquals(Surface.ROTATION_0, fromPortrait(301))
        assertEquals(Surface.ROTATION_90, fromPortrait(300))
    }

    @Test
    fun `hysteresis keeps landscape until 60 degrees from its center`() {
        // Landscape with the device angle at 90 (ROTATION_270).
        val landscape = Surface.ROTATION_270
        assertEquals(landscape, OrientationLogic.nextRotation(40, landscape))
        assertEquals(landscape, OrientationLogic.nextRotation(31, landscape))
        assertEquals(Surface.ROTATION_0, OrientationLogic.nextRotation(30, landscape))
        assertEquals(landscape, OrientationLogic.nextRotation(149, landscape))
        assertEquals(Surface.ROTATION_180, OrientationLogic.nextRotation(150, landscape))
    }

    @Test
    fun `a jitter around 45 degrees does not flip`() {
        var rotation = Surface.ROTATION_0
        for (angle in listOf(40, 50, 44, 55, 38, 52)) rotation = OrientationLogic.nextRotation(angle, rotation)
        assertEquals(Surface.ROTATION_0, rotation)
        for (angle in listOf(70, 40, 50, 35)) rotation = OrientationLogic.nextRotation(angle, rotation)
        assertEquals(Surface.ROTATION_270, rotation)
    }

    @Test
    fun `sideways rotations`() {
        assertEquals(false, OrientationLogic.isSideways(Surface.ROTATION_0))
        assertEquals(true, OrientationLogic.isSideways(Surface.ROTATION_90))
        assertEquals(false, OrientationLogic.isSideways(Surface.ROTATION_180))
        assertEquals(true, OrientationLogic.isSideways(Surface.ROTATION_270))
    }

    @Test
    fun `rotation request locks to degrees`() {
        assertEquals(Surface.ROTATION_0, OrientationLogic.lockedRotationFor(RotationRequest(degrees = 0)))
        assertEquals(Surface.ROTATION_90, OrientationLogic.lockedRotationFor(RotationRequest(degrees = 90)))
        assertEquals(Surface.ROTATION_180, OrientationLogic.lockedRotationFor(RotationRequest(degrees = 180)))
        assertEquals(Surface.ROTATION_270, OrientationLogic.lockedRotationFor(RotationRequest(degrees = 270)))
        assertEquals(null, OrientationLogic.lockedRotationFor(RotationRequest(auto = true)))
    }

    @Test
    fun `rotation request refuses bad values`() {
        val bad = listOf(
            RotationRequest(),
            RotationRequest(degrees = 90, auto = true),
            RotationRequest(auto = false),
            RotationRequest(degrees = 45),
            RotationRequest(degrees = 360),
            RotationRequest(degrees = -90)
        )
        for (request in bad) {
            val error = assertThrows(ApiException::class.java) {
                OrientationLogic.lockedRotationFor(request)
            }
            assertEquals(request.toString(), ErrorCode.BAD_REQUEST, error.code)
        }
    }

    @Test
    fun `surface degrees`() {
        assertEquals(0, OrientationLogic.surfaceDegrees(Surface.ROTATION_0))
        assertEquals(90, OrientationLogic.surfaceDegrees(Surface.ROTATION_90))
        assertEquals(180, OrientationLogic.surfaceDegrees(Surface.ROTATION_180))
        assertEquals(270, OrientationLogic.surfaceDegrees(Surface.ROTATION_270))
    }
}
