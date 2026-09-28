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
    val label: String,
    /** A short name for the badge (1-3 characters); a box without it gets the next free letter. */
    val tag: String? = null
)

/** An arrow at the preview edge: the target is outside the view in [angleDeg] (snapshot space, 0 = right, 90 = down). */
@Serializable
data class OverlayArrow(
    @SerialName("angle_deg")
    @Serializable(with = StrictFloatSerializer::class)
    val angleDeg: Float,
    val label: String,
    /** The same rule as a box tag; without it, the arrow gets the next free letter. */
    val tag: String? = null
)

/**
 * Either the shapes (`boxes` required, `arrows` optional: missing = no arrows) or `visible` alone
 * (see [OverlayLogic.command]). `ApiJson` refuses unknown fields.
 */
@Serializable
data class OverlayRequest(
    val boxes: List<OverlayBox>? = null,
    val arrows: List<OverlayArrow>? = null,
    @Serializable(with = StrictBooleanSerializer::class)
    val visible: Boolean? = null
)

/** What one `POST /v1/overlay` body does. */
sealed interface OverlayCommand {
    /** Replaces the boxes and arrows; the visibility stays. */
    data class SetShapes(val boxes: List<OverlayBox>, val arrows: List<OverlayArrow>) : OverlayCommand

    /** Hides or shows the overlay; the boxes and arrows stay. */
    data class SetVisible(val visible: Boolean) : OverlayCommand
}

/**
 * A box of the scene in view pixels, with its optional tag and its label (the layout input before the dp conversion).
 * [rect] is null when no part of the box is inside the view: the box keeps its place in the list, so its tag and
 * colour stay the same as on the page.
 */
data class LayoutItem(val rect: PixelRect?, val tag: String?, val label: String)

/** An arrow in view pixels: the tip near the preview edge and the tail towards the centre. */
data class ViewArrow(val tip: PixelPoint, val tail: PixelPoint)

/** An arrow of the scene in view pixels, with its optional tag and its label. */
data class SceneArrow(val arrow: ViewArrow, val tag: String?, val label: String)

/** What the overlay view draws: boxes and arrows in its pixels, and the viewer turn for upright labels. */
data class OverlayScene(val boxes: List<LayoutItem>, val arrows: List<SceneArrow>, val viewerDegrees: Int) {
    companion object {
        val EMPTY = OverlayScene(emptyList(), emptyList(), 0)
    }
}

/** A rectangle on the true-orientation snapshot, normalized to [0, 1]. */
@Serializable
data class PreviewRegion(
    @SerialName("snapshot_x") val snapshotX: Float,
    @SerialName("snapshot_y") val snapshotY: Float,
    val width: Float,
    val height: Float
)

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
    /**
     * Width and height of the upright preview image: the Preview stream, not the still (any unit, only the ratio
     * counts). With another aspect than the still, the preview is a centred crop of the still's field.
     */
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

    /**
     * The command of a body: `visible` alone, or the shapes with `boxes`. `visible` together with shapes, `arrows`
     * without `boxes`, or an empty body is 400. Throws [ApiException] with [ErrorCode.BAD_REQUEST].
     */
    fun command(request: OverlayRequest): OverlayCommand {
        val visible = request.visible
        if (visible != null) {
            if (request.boxes != null || request.arrows != null) bad(Constants.Messages.OVERLAY_VISIBLE_ALONE)
            return OverlayCommand.SetVisible(visible)
        }
        val boxes = request.boxes ?: bad(Constants.Messages.OVERLAY_NEEDS_BOXES)
        val command = OverlayCommand.SetShapes(boxes, request.arrows.orEmpty())
        validate(command)
        return command
    }

    /** Throws [ApiException] with [ErrorCode.BAD_REQUEST] when a rule of the contract fails. */
    fun validate(request: OverlayCommand.SetShapes) {
        if (request.boxes.size > Constants.Overlay.MAX_BOXES) bad(Constants.Messages.OVERLAY_TOO_MANY)
        for (box in request.boxes) {
            val values = listOf(box.snapshotX, box.snapshotY, box.width, box.height)
            if (values.any { !it.isFinite() }) bad(Constants.Messages.OVERLAY_BAD_BOX)
            val inside = box.snapshotX >= MIN && box.snapshotY >= MIN && box.width > MIN && box.height > MIN &&
                box.snapshotX + box.width <= MAX + Constants.Overlay.EDGE_TOLERANCE &&
                box.snapshotY + box.height <= MAX + Constants.Overlay.EDGE_TOLERANCE
            if (!inside) bad(Constants.Messages.OVERLAY_BAD_BOX)
            if (box.label.length > Constants.Overlay.MAX_LABEL_LENGTH) bad(Constants.Messages.OVERLAY_LABEL_TOO_LONG)
            checkTag(box.tag)
        }
        if (request.arrows.size > Constants.Overlay.MAX_ARROWS) bad(Constants.Messages.OVERLAY_TOO_MANY_ARROWS)
        for (arrow in request.arrows) {
            if (!arrow.angleDeg.isFinite()) bad(Constants.Messages.OVERLAY_BAD_ARROW)
            if (arrow.label.length > Constants.Overlay.MAX_LABEL_LENGTH) bad(Constants.Messages.OVERLAY_LABEL_TOO_LONG)
            checkTag(arrow.tag)
        }
    }

    private val tagPattern = Regex(Constants.Overlay.TAG_PATTERN)

    /** C7: a tag is 1-3 ASCII letters or digits (the same rule and count as the server). */
    private fun checkTag(tag: String?) {
        if (tag != null && !tagPattern.matches(tag)) bad(Constants.Messages.OVERLAY_BAD_TAG)
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
        val (captureX, captureY) = FocusTapLogic.snapshotToSurface(x, y, geometry.snapshotRotation)
        val (surfaceX, surfaceY) = captureToPreview(captureX, captureY, geometry)
        val (imageX, imageY) = surfaceToImage(surfaceX, surfaceY, geometry.previewRotation)
        return imageToView(imageX, imageY, geometry)
    }

    /** Width over height of the capture surface (the snapshot before its rotation). */
    private fun captureAspect(g: OverlayGeometry): Float =
        if (isSideways(g.snapshotRotation)) g.snapshotHeight / g.snapshotWidth else g.snapshotWidth / g.snapshotHeight

    /** Width over height of the preview surface (the upright preview image before its rotation). */
    private fun previewAspect(g: OverlayGeometry): Float =
        if (isSideways(g.previewRotation)) g.imageHeight / g.imageWidth else g.imageWidth / g.imageHeight

    private fun isSideways(degrees: Int): Boolean =
        Math.floorMod(degrees, 2 * Constants.Orientation.BUCKET_DEGREES) != 0

    /**
     * A normalized capture-surface point to the preview surface. With another aspect the preview stream is the
     * largest centred crop of the capture field (a wider preview loses the top and bottom, a narrower one the sides).
     */
    fun captureToPreview(x: Float, y: Float, geometry: OverlayGeometry): Pair<Float, Float> {
        val capture = captureAspect(geometry)
        val preview = previewAspect(geometry)
        return when {
            preview > capture -> (capture / preview).let { f -> x to (y - (MAX - f) / HALF) / f }
            preview < capture -> (preview / capture).let { f -> (x - (MAX - f) / HALF) / f to y }
            else -> x to y
        }
    }

    /** The inverse of [captureToPreview]. */
    fun previewToCapture(x: Float, y: Float, geometry: OverlayGeometry): Pair<Float, Float> {
        val capture = captureAspect(geometry)
        val preview = previewAspect(geometry)
        return when {
            preview > capture -> (capture / preview).let { f -> x to y * f + (MAX - f) / HALF }
            preview < capture -> (preview / capture).let { f -> x * f + (MAX - f) / HALF to y }
            else -> x to y
        }
    }

    /** A normalized point of the upright preview image to view pixels: the FILL crop or FIT letterbox, then the flips. */
    fun imageToView(imageX: Float, imageY: Float, geometry: OverlayGeometry): Pair<Float, Float> {
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
        val unflipped = geometry.copy(mirroredX = false, mirroredY = false)
        val (left, top) = imageToView(MIN, MIN, unflipped)
        val (right, bottom) = imageToView(MAX, MAX, unflipped)
        return PixelRect(
            maxOf(minOf(left, right), 0f),
            maxOf(minOf(top, bottom), 0f),
            minOf(maxOf(left, right), geometry.viewWidth),
            minOf(maxOf(top, bottom), geometry.viewHeight)
        )
    }

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

    /**
     * The part of the snapshot that the preview view shows. Zoom and flips do not change it: the preview and the
     * snapshot share the zoom crop, and a mirror maps the centred FILL crop onto itself. FIT shows the whole preview
     * image, which is the whole snapshot only when the preview and the still have the same aspect.
     */
    fun previewRegion(geometry: OverlayGeometry): PreviewRegion {
        val scaleX = geometry.viewWidth / geometry.imageWidth
        val scaleY = geometry.viewHeight / geometry.imageHeight
        val scale = if (geometry.fill) maxOf(scaleX, scaleY) else minOf(scaleX, scaleY)
        // The visible fraction of the upright preview image on each axis, centred.
        val visibleX = minOf(MAX, geometry.viewWidth / (geometry.imageWidth * scale))
        val visibleY = minOf(MAX, geometry.viewHeight / (geometry.imageHeight * scale))
        val corners = listOf(
            CENTRE - visibleX / HALF to CENTRE - visibleY / HALF,
            CENTRE + visibleX / HALF to CENTRE + visibleY / HALF
        ).map { (x, y) ->
            val (surfaceX, surfaceY) = FocusTapLogic.snapshotToSurface(x, y, geometry.previewRotation)
            val (captureX, captureY) = previewToCapture(surfaceX, surfaceY, geometry)
            surfaceToImage(captureX, captureY, geometry.snapshotRotation)
        }
        val left = corners.minOf { it.first }
        val top = corners.minOf { it.second }
        return PreviewRegion(left, top, corners.maxOf { it.first } - left, corners.maxOf { it.second } - top)
    }

    /** The inverse of [snapshotToView] at the current zoom: a view pixel to a normalized snapshot point. */
    fun viewToSnapshot(viewX: Float, viewY: Float, geometry: OverlayGeometry): Pair<Float, Float> {
        val x = if (geometry.mirroredX) geometry.viewWidth - viewX else viewX
        val y = if (geometry.mirroredY) geometry.viewHeight - viewY else viewY
        val scaleX = geometry.viewWidth / geometry.imageWidth
        val scaleY = geometry.viewHeight / geometry.imageHeight
        val scale = if (geometry.fill) maxOf(scaleX, scaleY) else minOf(scaleX, scaleY)
        val shownWidth = geometry.imageWidth * scale
        val shownHeight = geometry.imageHeight * scale
        val imageX = (x - (geometry.viewWidth - shownWidth) / HALF) / shownWidth
        val imageY = (y - (geometry.viewHeight - shownHeight) / HALF) / shownHeight
        val (surfaceX, surfaceY) = FocusTapLogic.snapshotToSurface(imageX, imageY, geometry.previewRotation)
        val (captureX, captureY) = previewToCapture(surfaceX, surfaceY, geometry)
        return surfaceToImage(captureX, captureY, geometry.snapshotRotation)
    }

    /**
     * `CameraStatus.overlay_region`: the part of the snapshot under [safeRect] (the phone view, in view pixels), inside
     * [previewRegion]. Unlike `preview_region` it can change with the flips: the safe area is not symmetric (a status
     * bar at the top, a navigation bar at the bottom). Null when nothing is left.
     */
    fun overlayRegion(safeRect: PixelRect, geometry: OverlayGeometry): PreviewRegion? {
        val a = viewToSnapshot(safeRect.left, safeRect.top, geometry)
        val b = viewToSnapshot(safeRect.right, safeRect.bottom, geometry)
        val preview = previewRegion(geometry)
        val left = maxOf(minOf(a.first, b.first), preview.snapshotX)
        val top = maxOf(minOf(a.second, b.second), preview.snapshotY)
        val right = minOf(maxOf(a.first, b.first), preview.snapshotX + preview.width)
        val bottom = minOf(maxOf(a.second, b.second), preview.snapshotY + preview.height)
        if (right <= left || bottom <= top) return null
        return PreviewRegion(left, top, right - left, bottom - top)
    }
}
