package dev.jayson.debugdevices.camera

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class OverlayLogicTest {
    /** The phone in portrait: surface turned 90 for both the snapshot and the preview, 3:4 upright image. */
    private val portrait = OverlayGeometry(
        zoomAtCall = 1f,
        zoomNow = 1f,
        snapshotRotation = 90,
        previewRotation = 90,
        imageWidth = 3f,
        imageHeight = 4f,
        viewWidth = 300f,
        viewHeight = 400f,
        fill = true,
        mirroredX = false,
        mirroredY = false
    )
    private val box = OverlayBox(snapshotX = 0.1f, snapshotY = 0.2f, width = 0.3f, height = 0.1f, label = "U1")
    private val delta = 1e-3f

    private fun assertRect(expected: PixelRect, actual: PixelRect?) {
        requireNotNull(actual)
        assertEquals(expected.left, actual.left, delta)
        assertEquals(expected.top, actual.top, delta)
        assertEquals(expected.right, actual.right, delta)
        assertEquals(expected.bottom, actual.bottom, delta)
    }

    @Test
    fun `same rotation and aspect maps straight`() {
        assertRect(PixelRect(30f, 80f, 120f, 120f), OverlayLogic.boxToView(box, portrait))
    }

    @Test
    fun `surface to image is the inverse of snapshot to surface`() {
        for (degrees in listOf(0, 90, 180, 270)) {
            val (sx, sy) = FocusTapLogic.snapshotToSurface(0.1f, 0.3f, degrees)
            val (x, y) = OverlayLogic.surfaceToImage(sx, sy, degrees)
            assertEquals("$degrees", 0.1f, x, delta)
            assertEquals("$degrees", 0.3f, y, delta)
        }
    }

    @Test
    fun `a landscape snapshot on the portrait preview`() {
        // Phone sideways: the snapshot is the surface turned 0, the preview the surface turned 90.
        val sideways = portrait.copy(snapshotRotation = 0)
        // Snapshot top-left corner = surface top-left = preview top-right.
        val corner = OverlayLogic.snapshotToView(0f, 0f, sideways)
        assertEquals(300f, corner.first, delta)
        assertEquals(0f, corner.second, delta)
    }

    @Test
    fun `preview flips mirror the box`() {
        assertRect(PixelRect(180f, 80f, 270f, 120f), OverlayLogic.boxToView(box, portrait.copy(mirroredX = true)))
        assertRect(PixelRect(30f, 280f, 120f, 320f), OverlayLogic.boxToView(box, portrait.copy(mirroredY = true)))
    }

    @Test
    fun `zoom scales the box around the centre`() {
        assertEquals(0.5f, OverlayLogic.rescale(0.5f, 1f, 2f), delta)
        assertEquals(0.3f, OverlayLogic.rescale(0.4f, 1f, 2f), delta)
        assertEquals(0.45f, OverlayLogic.rescale(0.4f, 2f, 1f), delta)
        // x 0.1..0.4 at 1x -> -0.3..0.3 at 2x: still partly inside.
        assertRect(PixelRect(-90f, -40f, 90f, 40f), OverlayLogic.boxToView(box, portrait.copy(zoomNow = 2f)))
    }

    @Test
    fun `a box that leaves the view is hidden`() {
        val corner = OverlayBox(0f, 0f, 0.1f, 0.1f, "edge")
        assertNull(OverlayLogic.boxToView(corner, portrait.copy(zoomNow = 4f)))
    }

    @Test
    fun `fill crops and fit letterboxes a tall view`() {
        // The phone: view 1280 x 2772 for a 3:4 image. FILL scales by the height and crops the sides.
        val phone = portrait.copy(viewWidth = 1280f, viewHeight = 2772f)
        val centre = OverlayBox(0.45f, 0.45f, 0.1f, 0.1f, "c")
        val shown = 2772f * 3f / 4f
        val offset = (1280f - shown) / 2f
        assertRect(
            PixelRect(offset + 0.45f * shown, 0.45f * 2772f, offset + 0.55f * shown, 0.55f * 2772f),
            OverlayLogic.boxToView(centre, phone)
        )
        val fitTop = (2772f - 1280f * 4f / 3f) / 2f
        val fit = OverlayLogic.boxToView(centre, phone.copy(fill = false))
        assertRect(
            PixelRect(
                0.45f * 1280f,
                fitTop + 0.45f * 1280f * 4f / 3f,
                0.55f * 1280f,
                fitTop + 0.55f * 1280f * 4f / 3f
            ),
            fit
        )
        // A box at the left edge is cut off by the FILL crop.
        assertNull(OverlayLogic.boxToView(OverlayBox(0f, 0.4f, 0.1f, 0.1f, "cut"), phone))
    }
}
