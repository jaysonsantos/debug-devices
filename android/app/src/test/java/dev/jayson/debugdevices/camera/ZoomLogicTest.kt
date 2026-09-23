package dev.jayson.debugdevices.camera

import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Test

class ZoomLogicTest {
    private val status = CameraStatus(
        zoomRatio = 2f,
        minZoomRatio = 1f,
        maxZoomRatio = 8f,
        torchEnabled = false,
        hasFlashUnit = true,
    )

    @Test
    fun `step factor is 1_5`() {
        assertEquals(1.5f, Constants.Zoom.STEP_FACTOR)
    }

    @Test
    fun `step in multiplies by the factor`() {
        assertEquals(3f, ZoomLogic.step(2f, ZoomStep.IN, 1f, 8f), DELTA)
    }

    @Test
    fun `step out divides by the factor`() {
        assertEquals(2f, ZoomLogic.step(3f, ZoomStep.OUT, 1f, 8f), DELTA)
    }

    @Test
    fun `step in clamps to max`() {
        assertEquals(8f, ZoomLogic.step(6f, ZoomStep.IN, 1f, 8f), DELTA)
    }

    @Test
    fun `step out clamps to min`() {
        assertEquals(1f, ZoomLogic.step(1.2f, ZoomStep.OUT, 1f, 8f), DELTA)
    }

    @Test
    fun `step works with a min below 1`() {
        assertEquals(0.6f, ZoomLogic.step(0.7f, ZoomStep.OUT, 0.6f, 10f), DELTA)
    }

    @Test
    fun `explicit ratio inside the range is kept`() {
        assertEquals(2.5f, ZoomLogic.resolve(ZoomRequest(ratio = 2.5f), status), DELTA)
    }

    @Test
    fun `explicit ratio above max is clamped`() {
        assertEquals(8f, ZoomLogic.resolve(ZoomRequest(ratio = 50f), status), DELTA)
    }

    @Test
    fun `explicit ratio below min is clamped`() {
        assertEquals(1f, ZoomLogic.resolve(ZoomRequest(ratio = 0.1f), status), DELTA)
    }

    @Test
    fun `resolve step uses the current ratio`() {
        assertEquals(3f, ZoomLogic.resolve(ZoomRequest(step = ZoomStep.IN), status), DELTA)
        assertEquals(4f / 3f, ZoomLogic.resolve(ZoomRequest(step = ZoomStep.OUT), status), DELTA)
    }

    @Test
    fun `resolve refuses both fields`() {
        val error = assertThrows(ApiException::class.java) {
            ZoomLogic.resolve(ZoomRequest(ratio = 2f, step = ZoomStep.IN), status)
        }
        assertEquals(ErrorCode.BAD_REQUEST, error.code)
    }

    @Test
    fun `resolve refuses an empty request`() {
        val error = assertThrows(ApiException::class.java) { ZoomLogic.resolve(ZoomRequest(), status) }
        assertEquals(ErrorCode.BAD_REQUEST, error.code)
    }

    @Test
    fun `resolve refuses a non finite ratio`() {
        val error = assertThrows(ApiException::class.java) {
            ZoomLogic.resolve(ZoomRequest(ratio = Float.NaN), status)
        }
        assertEquals(ErrorCode.BAD_REQUEST, error.code)
    }

    private companion object {
        const val DELTA = 1e-4f
    }
}
