package dev.jayson.debugdevices.camera

import android.view.Surface
import org.junit.Assert.assertEquals
import org.junit.Test

class PreviewFlipLogicTest {
    private val none = PreviewScale(1f, 1f)
    private val mirrorX = PreviewScale(-1f, 1f)
    private val mirrorY = PreviewScale(1f, -1f)
    private val both = PreviewScale(-1f, -1f)
    private val h = PreviewFlip(horizontal = true, vertical = false)
    private val v = PreviewFlip(horizontal = false, vertical = true)
    private val hv = PreviewFlip(horizontal = true, vertical = true)

    @Test
    fun `no flip after start`() {
        assertEquals(PreviewFlip(horizontal = false, vertical = false), PreviewFlip.NONE)
        for (rotation in listOf(Surface.ROTATION_0, Surface.ROTATION_90, Surface.ROTATION_180, Surface.ROTATION_270)) {
            assertEquals(none, PreviewFlipLogic.scale(PreviewFlip.NONE, rotation))
        }
    }

    @Test
    fun `upright portrait maps directly`() {
        assertEquals(mirrorX, PreviewFlipLogic.scale(h, Surface.ROTATION_0))
        assertEquals(mirrorY, PreviewFlipLogic.scale(v, Surface.ROTATION_0))
        assertEquals(both, PreviewFlipLogic.scale(hv, Surface.ROTATION_0))
        assertEquals(mirrorX, PreviewFlipLogic.scale(h, Surface.ROTATION_180))
    }

    @Test
    fun `sideways swaps the axes`() {
        for (rotation in listOf(Surface.ROTATION_90, Surface.ROTATION_270)) {
            assertEquals(mirrorY, PreviewFlipLogic.scale(h, rotation))
            assertEquals(mirrorX, PreviewFlipLogic.scale(v, rotation))
            assertEquals(both, PreviewFlipLogic.scale(hv, rotation))
        }
    }

    @Test
    fun `request maps to the flip`() {
        assertEquals(h, PreviewFlipLogic.fromRequest(PreviewRequest(flipHorizontal = true, flipVertical = false)))
        assertEquals(v, PreviewFlipLogic.fromRequest(PreviewRequest(flipHorizontal = false, flipVertical = true)))
    }
}
