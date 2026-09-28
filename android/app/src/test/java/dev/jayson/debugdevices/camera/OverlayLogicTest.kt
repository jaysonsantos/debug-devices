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
        snapshotWidth = 3060f,
        snapshotHeight = 4080f,
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

    @Test
    fun `arrow direction in portrait with and without flips`() {
        fun dir(angle: Float, g: OverlayGeometry) = OverlayLogic.arrowDirection(angle, g)
        assertPoint(PixelPoint(1f, 0f), dir(0f, portrait))
        assertPoint(PixelPoint(0f, 1f), dir(90f, portrait))
        assertPoint(PixelPoint(-1f, 0f), dir(180f, portrait))
        assertPoint(PixelPoint(0f, -1f), dir(270f, portrait))
        assertPoint(PixelPoint(0f, -1f), dir(-90f, portrait))
        assertPoint(PixelPoint(-1f, 0f), dir(0f, portrait.copy(mirroredX = true)))
        assertPoint(PixelPoint(0f, -1f), dir(90f, portrait.copy(mirroredY = true)))
    }

    @Test
    fun `arrow direction for each snapshot rotation`() {
        // Snapshot turned 0 from the surface (phone sideways): snapshot right = surface right = preview down.
        assertPoint(
            PixelPoint(0f, 1f),
            OverlayLogic.arrowDirection(
                0f,
                portrait.copy(snapshotRotation = 0, snapshotWidth = 4080f, snapshotHeight = 3060f)
            )
        )
        // 180: snapshot right = surface left = preview up.
        assertPoint(
            PixelPoint(0f, -1f),
            OverlayLogic.arrowDirection(
                0f,
                portrait.copy(snapshotRotation = 180, snapshotWidth = 4080f, snapshotHeight = 3060f)
            )
        )
        // 270: upside down against the preview: snapshot right = preview left.
        assertPoint(PixelPoint(-1f, 0f), OverlayLogic.arrowDirection(0f, portrait.copy(snapshotRotation = 270)))
    }

    @Test
    fun `a diagonal keeps its angle in pixels`() {
        // 45 degrees in snapshot pixels stays 45 degrees on the view (uniform scale).
        val d = OverlayLogic.arrowDirection(45f, portrait)
        assertEquals(d.x, d.y, delta)
    }

    @Test
    fun `arrow at the edge of the shown area`() {
        val area = PixelRect(0f, 0f, 300f, 400f)
        val right = OverlayLogic.arrowAtEdge(PixelPoint(1f, 0f), area, inset = 10f, length = 50f)
        assertPoint(PixelPoint(290f, 200f), right.tip)
        assertPoint(PixelPoint(240f, 200f), right.tail)
        val up = OverlayLogic.arrowAtEdge(PixelPoint(0f, -1f), area, inset = 10f, length = 50f)
        assertPoint(PixelPoint(150f, 10f), up.tip)
        // The diagonal of a tall area leaves through the side.
        val s = (1 / Math.sqrt(2.0)).toFloat()
        val diagonal = OverlayLogic.arrowAtEdge(PixelPoint(s, s), area, inset = 0f, length = 0f)
        assertPoint(PixelPoint(300f, 350f), diagonal.tip)
    }

    @Test
    fun `shown area is the view for FILL and the letterbox for FIT`() {
        val phone = portrait.copy(viewWidth = 1280f, viewHeight = 2772f)
        val fill = OverlayLogic.shownArea(phone)
        assertEquals(PixelRect(0f, 0f, 1280f, 2772f), fill)
        val fit = OverlayLogic.shownArea(phone.copy(fill = false))
        val top = (2772f - 1280f * 4f / 3f) / 2f
        assertEquals(top, fit.top, delta)
        assertEquals(2772f - top, fit.bottom, delta)
    }

    @Test
    fun `preview region of a tall portrait view with a 3-4 image`() {
        // A 9:20 screen: only the middle ~0.6 of the width is on the screen, the full height.
        val tall = portrait.copy(viewWidth = 900f, viewHeight = 2000f)
        val region = OverlayLogic.previewRegion(tall)
        val visible = 900f / (2000f * 3f / 4f)
        assertEquals(0.6f, visible, delta)
        assertEquals((1f - visible) / 2f, region.snapshotX, delta)
        assertEquals(visible, region.width, delta)
        assertEquals(0f, region.snapshotY, delta)
        assertEquals(1f, region.height, delta)
    }

    @Test
    fun `preview region of a landscape snapshot`() {
        // Phone sideways: the snapshot is the surface (landscape), the preview the surface turned 90.
        val sideways = portrait.copy(viewWidth = 900f, viewHeight = 2000f, snapshotRotation = 0)
        val region = OverlayLogic.previewRegion(sideways)
        assertEquals(0f, region.snapshotX, delta)
        assertEquals(1f, region.width, delta)
        assertEquals(0.2f, region.snapshotY, delta)
        assertEquals(0.6f, region.height, delta)
        // Upside down (270): the same centred band.
        val upsideDown = OverlayLogic.previewRegion(sideways.copy(snapshotRotation = 180))
        assertEquals(0.2f, upsideDown.snapshotY, delta)
        assertEquals(0.6f, upsideDown.height, delta)
    }

    @Test
    fun `preview region does not change with flips or zoom, and FIT shows everything`() {
        val tall = portrait.copy(viewWidth = 900f, viewHeight = 2000f)
        val base = OverlayLogic.previewRegion(tall)
        assertEquals(base, OverlayLogic.previewRegion(tall.copy(mirroredX = true, mirroredY = true)))
        assertEquals(base, OverlayLogic.previewRegion(tall.copy(zoomNow = 3f)))
        assertEquals(PreviewRegion(0f, 0f, 1f, 1f), OverlayLogic.previewRegion(tall.copy(fill = false)))
        // A view with the image's own aspect shows everything.
        assertEquals(PreviewRegion(0f, 0f, 1f, 1f), OverlayLogic.previewRegion(portrait))
    }

    private fun assertPoint(expected: PixelPoint, actual: PixelPoint) {
        assertEquals(expected.x, actual.x, delta)
        assertEquals(expected.y, actual.y, delta)
    }
}
