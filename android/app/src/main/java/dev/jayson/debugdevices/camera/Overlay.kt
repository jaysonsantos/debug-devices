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

/** An arrow at the preview edge: the target is outside the view in [angleDeg] (snapshot space, 0 = right, 90 = down). */
@Serializable
data class OverlayArrow(
    @SerialName("angle_deg")
    @Serializable(with = StrictFloatSerializer::class)
    val angleDeg: Float,
    val label: String
)

/** `boxes` is required; `arrows` is optional (missing = no arrows). `ApiJson` refuses unknown fields. */
@Serializable
data class OverlayRequest(val boxes: List<OverlayBox>, val arrows: List<OverlayArrow> = emptyList())

/** An arrow in view pixels: the tip near the preview edge and the tail towards the centre. */
data class ViewArrow(val tip: PixelPoint, val tail: PixelPoint)

/** What the overlay view draws: boxes and arrows in its pixels, and the viewer turn for upright labels. */
data class OverlayScene(
    val boxes: List<Pair<PixelRect, String>>,
    val arrows: List<Pair<ViewArrow, String>>,
    val viewerDegrees: Int
) {
    companion object {
        val EMPTY = OverlayScene(emptyList(), emptyList(), 0)
    }
}

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
    /** Width and height of the snapshot in pixels: an arrow angle is a direction in these pixels. */
    val snapshotWidth: Float,
    val snapshotHeight: Float,
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
        if (request.arrows.size > Constants.Overlay.MAX_ARROWS) bad(Constants.Messages.OVERLAY_TOO_MANY_ARROWS)
        for (arrow in request.arrows) {
            if (!arrow.angleDeg.isFinite()) bad(Constants.Messages.OVERLAY_BAD_ARROW)
            if (arrow.label.length > Constants.Overlay.MAX_LABEL_LENGTH) bad(Constants.Messages.OVERLAY_LABEL_TOO_LONG)
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

    /** The shown preview image in view pixels: the whole view for FILL, the letterbox for FIT. */
    fun shownArea(geometry: OverlayGeometry): PixelRect {
        val (left, top) = snapshotToViewUnflipped(MIN, MIN, geometry.copy(snapshotRotation = geometry.previewRotation))
        val (right, bottom) = snapshotToViewUnflipped(
            MAX,
            MAX,
            geometry.copy(snapshotRotation = geometry.previewRotation)
        )
        return PixelRect(
            maxOf(minOf(left, right), 0f),
            maxOf(minOf(top, bottom), 0f),
            minOf(maxOf(left, right), geometry.viewWidth),
            minOf(maxOf(top, bottom), geometry.viewHeight)
        )
    }

    private fun snapshotToViewUnflipped(x: Float, y: Float, geometry: OverlayGeometry) =
        snapshotToView(x, y, geometry.copy(mirroredX = false, mirroredY = false))

    /** The unit direction in view pixels of a snapshot angle, after rotation, aspect, and flips. */
    fun arrowDirection(angleDeg: Float, geometry: OverlayGeometry): PixelPoint {
        val radians = Math.toRadians(angleDeg.toDouble())
        // A small step in snapshot pixels, as normalized coordinates.
        val stepX = (Math.cos(radians) / geometry.snapshotWidth).toFloat() * Constants.Overlay.DIRECTION_STEP
        val stepY = (Math.sin(radians) / geometry.snapshotHeight).toFloat() * Constants.Overlay.DIRECTION_STEP
        val flat = geometry.copy(zoomAtCall = MAX, zoomNow = MAX)
        val (cx, cy) = snapshotToView(CENTRE, CENTRE, flat)
        val (px, py) = snapshotToView(CENTRE + stepX, CENTRE + stepY, flat)
        val length = Math.hypot((px - cx).toDouble(), (py - cy).toDouble()).toFloat()
        return PixelPoint((px - cx) / length, (py - cy) / length)
    }

    /**
     * Where the ray from the centre of [area] in [direction] leaves it, pulled [inset] pixels back towards the
     * centre, and the tail [length] pixels further in.
     */
    fun arrowAtEdge(direction: PixelPoint, area: PixelRect, inset: Float, length: Float): ViewArrow {
        val cx = (area.left + area.right) / HALF
        val cy = (area.top + area.bottom) / HALF
        val tx = if (direction.x ==
            0f
        ) {
            Float.POSITIVE_INFINITY
        } else {
            ((area.right - area.left) / HALF) / Math.abs(direction.x)
        }
        val ty = if (direction.y ==
            0f
        ) {
            Float.POSITIVE_INFINITY
        } else {
            ((area.bottom - area.top) / HALF) / Math.abs(direction.y)
        }
        val toEdge = minOf(tx, ty)
        val tipDistance = (toEdge - inset).coerceAtLeast(0f)
        val tailDistance = (tipDistance - length).coerceAtLeast(0f)
        return ViewArrow(
            tip = PixelPoint(cx + direction.x * tipDistance, cy + direction.y * tipDistance),
            tail = PixelPoint(cx + direction.x * tailDistance, cy + direction.y * tailDistance)
        )
    }

    /** The corner of [rect] that is the top left for a viewer who sees the screen turned by [viewerDegrees]. */
    fun viewerTopLeft(rect: PixelRect, viewerDegrees: Int): PixelPoint =
        when (Math.floorMod(viewerDegrees, Constants.Orientation.DEGREES_PER_TURN)) {
            Constants.Orientation.BUCKET_DEGREES -> PixelPoint(rect.right, rect.top)
            2 * Constants.Orientation.BUCKET_DEGREES -> PixelPoint(rect.right, rect.bottom)
            3 * Constants.Orientation.BUCKET_DEGREES -> PixelPoint(rect.left, rect.bottom)
            else -> PixelPoint(rect.left, rect.top)
        }

    /**
     * The screen rectangle of a label of [width] x [height], drawn from [anchor] turned clockwise by
     * [viewerDegrees]; in its own frame it spans (0, -height) to (width, 0), so it sits above the anchor.
     */
    fun labelRect(anchor: PixelPoint, width: Float, height: Float, viewerDegrees: Int): PixelRect {
        val corners = listOf(0f to -height, width to -height, 0f to 0f, width to 0f).map { (x, y) ->
            rotate(x, y, viewerDegrees)
        }
        return PixelRect(
            anchor.x + corners.minOf { it.first },
            anchor.y + corners.minOf { it.second },
            anchor.x + corners.maxOf { it.first },
            anchor.y + corners.maxOf { it.second }
        )
    }

    /** The shift that moves [rect] inside a view of [width] x [height] (when it fits). */
    fun shiftInside(rect: PixelRect, width: Float, height: Float): PixelPoint {
        val dx = when {
            rect.left < 0f -> -rect.left
            rect.right > width -> width - rect.right
            else -> 0f
        }
        val dy = when {
            rect.top < 0f -> -rect.top
            rect.bottom > height -> height - rect.bottom
            else -> 0f
        }
        return PixelPoint(dx, dy)
    }

    /** Turns a vector clockwise on the screen (y down) by a multiple of 90 degrees. */
    private fun rotate(x: Float, y: Float, degrees: Int): Pair<Float, Float> =
        when (Math.floorMod(degrees, Constants.Orientation.DEGREES_PER_TURN)) {
            Constants.Orientation.BUCKET_DEGREES -> -y to x
            2 * Constants.Orientation.BUCKET_DEGREES -> -x to -y
            3 * Constants.Orientation.BUCKET_DEGREES -> y to -x
            else -> x to y
        }
}
