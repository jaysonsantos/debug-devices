package dev.jayson.debugdevices.camera

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/** One highlight box on the true-orientation snapshot, normalized to [0, 1]. */
@Serializable
data class OverlayBox(
    @SerialName("snapshot_x")
    @Serializable(with = StrictFloatSerializer::class)
    val snapshotX: Float,
    @SerialName("snapshot_y")
    @Serializable(with = StrictFloatSerializer::class)
    val snapshotY: Float,
    @Serializable(with = StrictFloatSerializer::class)
    val width: Float,
    @Serializable(with = StrictFloatSerializer::class)
    val height: Float,
    val label: String
)

/** `{"boxes": []}` clears. The field is required. `ApiJson` refuses unknown fields. */
@Serializable
data class OverlayRequest(val boxes: List<OverlayBox>)

/** A rectangle in view pixels. */
data class PixelRect(val left: Float, val top: Float, val right: Float, val bottom: Float)

/** Everything that places the boxes on the preview view, except the boxes. */
data class OverlayGeometry(
    val zoomAtCall: Float,
    val zoomNow: Float,
    /** Clockwise turn from the capture surface to the snapshot. */
    val snapshotRotation: Int,
    /** Clockwise turn from the capture surface to the upright preview (portrait display). */
    val previewRotation: Int,
    /** Width and height of the upright preview image (any unit, only the ratio counts). */
    val imageWidth: Float,
    val imageHeight: Float,
    val viewWidth: Float,
    val viewHeight: Float,
    /** True for FILL (crop to fill the view), false for FIT (letterbox). */
    val fill: Boolean,
    val mirroredX: Boolean,
    val mirroredY: Boolean
)

/** Pure rules of `POST /v1/overlay`, so JVM unit tests cover them. */
object OverlayLogic {
    private const val MIN = 0f
    private const val MAX = 1f
    private const val CENTRE = 0.5f
    private const val HALF = 2f

    /** Throws [ApiException] with [ErrorCode.BAD_REQUEST] when a rule of the contract fails. */
    fun validate(request: OverlayRequest) {
        if (request.boxes.size > Constants.Overlay.MAX_BOXES) bad(Constants.Messages.OVERLAY_TOO_MANY)
        for (box in request.boxes) {
            val values = listOf(box.snapshotX, box.snapshotY, box.width, box.height)
            if (values.any { !it.isFinite() }) bad(Constants.Messages.OVERLAY_BAD_BOX)
            val inside = box.snapshotX >= MIN && box.snapshotY >= MIN && box.width > MIN && box.height > MIN &&
                box.snapshotX + box.width <= MAX + Constants.Overlay.EDGE_TOLERANCE &&
                box.snapshotY + box.height <= MAX + Constants.Overlay.EDGE_TOLERANCE
            if (!inside) bad(Constants.Messages.OVERLAY_BAD_BOX)
            if (box.label.length > Constants.Overlay.MAX_LABEL_LENGTH) bad(Constants.Messages.OVERLAY_LABEL_TOO_LONG)
        }
    }

    private fun bad(message: String): Nothing = throw ApiException(ErrorCode.BAD_REQUEST, message)

    /** Zoom crops around the centre, so a point moves away from the centre by the zoom change since the call. */
    fun rescale(value: Float, zoomAtCall: Float, zoomNow: Float): Float =
        CENTRE + (value - CENTRE) * (zoomNow / zoomAtCall)

    /** A normalized surface point turned clockwise by [degrees]: the inverse of [FocusTapLogic.snapshotToSurface]. */
    fun surfaceToImage(x: Float, y: Float, degrees: Int): Pair<Float, Float> =
        when (Math.floorMod(degrees, Constants.Orientation.DEGREES_PER_TURN)) {
            Constants.Orientation.BUCKET_DEGREES -> MAX - y to x
            2 * Constants.Orientation.BUCKET_DEGREES -> MAX - x to MAX - y
            3 * Constants.Orientation.BUCKET_DEGREES -> y to MAX - x
            else -> x to y
        }

    /** A snapshot point (already rescaled) to preview view pixels. */
    fun snapshotToView(x: Float, y: Float, geometry: OverlayGeometry): Pair<Float, Float> {
        val (surfaceX, surfaceY) = FocusTapLogic.snapshotToSurface(x, y, geometry.snapshotRotation)
        val (imageX, imageY) = surfaceToImage(surfaceX, surfaceY, geometry.previewRotation)
        val scaleX = geometry.viewWidth / geometry.imageWidth
        val scaleY = geometry.viewHeight / geometry.imageHeight
        val scale = if (geometry.fill) maxOf(scaleX, scaleY) else minOf(scaleX, scaleY)
        val shownWidth = geometry.imageWidth * scale
        val shownHeight = geometry.imageHeight * scale
        val viewX = (geometry.viewWidth - shownWidth) / HALF + imageX * shownWidth
        val viewY = (geometry.viewHeight - shownHeight) / HALF + imageY * shownHeight
        return (if (geometry.mirroredX) geometry.viewWidth - viewX else viewX) to
            (if (geometry.mirroredY) geometry.viewHeight - viewY else viewY)
    }

    /** The box on the view, or null when no part of it is inside the view. */
    fun boxToView(box: OverlayBox, geometry: OverlayGeometry): PixelRect? {
        val left = rescale(box.snapshotX, geometry.zoomAtCall, geometry.zoomNow)
        val top = rescale(box.snapshotY, geometry.zoomAtCall, geometry.zoomNow)
        val right = rescale(box.snapshotX + box.width, geometry.zoomAtCall, geometry.zoomNow)
        val bottom = rescale(box.snapshotY + box.height, geometry.zoomAtCall, geometry.zoomNow)
        val a = snapshotToView(left, top, geometry)
        val b = snapshotToView(right, bottom, geometry)
        val rect =
            PixelRect(
                minOf(a.first, b.first),
                minOf(a.second, b.second),
                maxOf(a.first, b.first),
                maxOf(a.second, b.second)
            )
        val visible =
            rect.right > 0f && rect.bottom > 0f && rect.left < geometry.viewWidth && rect.top < geometry.viewHeight
        return rect.takeIf { visible }
    }
}
