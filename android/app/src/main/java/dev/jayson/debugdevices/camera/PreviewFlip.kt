package dev.jayson.debugdevices.camera

/** Mirror of the on-screen camera preview only. Snapshots never change. Both false after an app start. */
data class PreviewFlip(val horizontal: Boolean, val vertical: Boolean) {
    companion object {
        val NONE = PreviewFlip(horizontal = false, vertical = false)
    }
}

/** View scale factors for the preview. */
data class PreviewScale(val scaleX: Float, val scaleY: Float)

/** Pure rules for the preview flip, so JVM unit tests cover them. */
object PreviewFlipLogic {
    private const val NORMAL = 1f
    private const val MIRRORED = -1f

    fun fromRequest(request: PreviewRequest): PreviewFlip =
        PreviewFlip(horizontal = request.flipHorizontal, vertical = request.flipVertical)

    /**
     * The flips are in the upright frame of the viewer, like the snapshot. The activity stays in portrait, so when
     * the phone is sideways the viewer's left-right axis is the screen's Y axis: the two axes swap.
     */
    fun scale(flip: PreviewFlip, rotation: Int): PreviewScale {
        val sideways = OrientationLogic.isSideways(rotation)
        val mirrorScreenX = if (sideways) flip.vertical else flip.horizontal
        val mirrorScreenY = if (sideways) flip.horizontal else flip.vertical
        return PreviewScale(
            scaleX = if (mirrorScreenX) MIRRORED else NORMAL,
            scaleY = if (mirrorScreenY) MIRRORED else NORMAL
        )
    }
}
