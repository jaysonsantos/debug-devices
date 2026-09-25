package dev.jayson.debugdevices.camera

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Test

class FocusTapLogicTest {
    /** The phone: display 1280 x 2772, the preview fills it. */
    private val full = PreviewGeometry(1280, 2772, 0, 0, 1280, 2772, mirroredX = false, mirroredY = false)

    private fun badRequest(request: FocusRequest) {
        val error = assertThrows(ApiException::class.java) { FocusTapLogic.target(request) }
        assertEquals(request.toString(), ErrorCode.BAD_REQUEST, error.code)
    }

    @Test
    fun `exactly one complete pair`() {
        assertEquals(FocusTarget.Screen(0.4f, 0.6f), FocusTapLogic.target(FocusRequest(screenX = 0.4f, screenY = 0.6f)))
        assertEquals(FocusTarget.Snapshot(0f, 1f), FocusTapLogic.target(FocusRequest(snapshotX = 0f, snapshotY = 1f)))
        badRequest(FocusRequest())
        badRequest(FocusRequest(screenX = 0.4f))
        badRequest(FocusRequest(snapshotY = 0.4f))
        badRequest(FocusRequest(screenX = 0.4f, screenY = 0.6f, snapshotX = 0.1f, snapshotY = 0.1f))
        badRequest(FocusRequest(screenX = 0.4f, screenY = 0.6f, snapshotX = 0.1f))
        badRequest(FocusRequest(screenX = 0.4f, snapshotY = 0.6f))
    }

    @Test
    fun `values must be in 0 to 1`() {
        badRequest(FocusRequest(screenX = -0.01f, screenY = 0.5f))
        badRequest(FocusRequest(screenX = 0.5f, screenY = 1.01f))
        badRequest(FocusRequest(snapshotX = Float.NaN, snapshotY = 0.5f))
        badRequest(FocusRequest(snapshotX = 0.5f, snapshotY = Float.POSITIVE_INFINITY))
    }

    @Test
    fun `screen point maps to preview pixels`() {
        assertEquals(PixelPoint(640f, 1386f), FocusTapLogic.screenToPreview(0.5f, 0.5f, full))
        assertEquals(PixelPoint(0f, 0f), FocusTapLogic.screenToPreview(0f, 0f, full))
        assertEquals(PixelPoint(1280f, 2772f), FocusTapLogic.screenToPreview(1f, 1f, full))
    }

    @Test
    fun `preview flips are undone`() {
        val mirrored = full.copy(mirroredX = true)
        assertEquals(PixelPoint(1280f - 320f, 693f), FocusTapLogic.screenToPreview(0.25f, 0.25f, mirrored))
        val upsideDown = full.copy(mirroredY = true)
        assertEquals(PixelPoint(320f, 2772f - 693f), FocusTapLogic.screenToPreview(0.25f, 0.25f, upsideDown))
        val both = full.copy(mirroredX = true, mirroredY = true)
        assertEquals(PixelPoint(960f, 2079f), FocusTapLogic.screenToPreview(0.25f, 0.25f, both))
    }

    @Test
    fun `a preview smaller than the display, with an offset`() {
        // The preview sits below a 100 px bar and is 1000 px tall.
        val boxed = full.copy(viewTop = 100, viewHeight = 1000)
        // 0.25 * 2772 = 693 px on the display, 593 px inside the preview.
        assertEquals(PixelPoint(640f, 593f), FocusTapLogic.screenToPreview(0.5f, 0.25f, boxed))
        assertNull(FocusTapLogic.screenToPreview(0.5f, 0.01f, boxed))
        assertNull(FocusTapLogic.screenToPreview(0.5f, 0.5f, boxed))
    }

    @Test
    fun `snapshot point back to the capture surface for each rotation`() {
        // A known point: the top-left corner of the upright snapshot.
        assertEquals(0f to 0f, FocusTapLogic.snapshotToSurface(0f, 0f, 0))
        // 90: the surface was turned clockwise, so the snapshot top-left was the surface bottom-left.
        assertEquals(0f to 1f, FocusTapLogic.snapshotToSurface(0f, 0f, 90))
        assertEquals(1f to 1f, FocusTapLogic.snapshotToSurface(0f, 0f, 180))
        assertEquals(1f to 0f, FocusTapLogic.snapshotToSurface(0f, 0f, 270))
        assertEquals(0.5f to 0.5f, FocusTapLogic.snapshotToSurface(0.5f, 0.5f, 90))
        assertEquals(0.25f to 0.9f, FocusTapLogic.snapshotToSurface(0.1f, 0.25f, 90))
        assertEquals(0.75f to 0.1f, FocusTapLogic.snapshotToSurface(0.1f, 0.25f, 270))
        assertEquals(0.25f to 0.9f, FocusTapLogic.snapshotToSurface(0.1f, 0.25f, 450))
    }
}
