package dev.jayson.debugdevices.camera

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/** Exactly one pair is required. `ApiJson` refuses unknown fields. */
@Serializable
data class FocusRequest(
    @SerialName("screen_x")
    @Serializable(with = StrictFloatSerializer::class)
    val screenX: Float? = null,
    @SerialName("screen_y")
    @Serializable(with = StrictFloatSerializer::class)
    val screenY: Float? = null,
    @SerialName("snapshot_x")
    @Serializable(with = StrictFloatSerializer::class)
    val snapshotX: Float? = null,
    @SerialName("snapshot_y")
    @Serializable(with = StrictFloatSerializer::class)
    val snapshotY: Float? = null
)

/** Where to focus: a normalized point on the phone screen or on the current snapshot. */
sealed interface FocusTarget {
    data class Screen(val x: Float, val y: Float) : FocusTarget

    data class Snapshot(val x: Float, val y: Float) : FocusTarget
}

/** A point in pixels. */
data class PixelPoint(val x: Float, val y: Float)

/** Where the preview view is on the display, in pixels, and its view mirroring (scale -1 = mirrored). */
data class PreviewGeometry(
    val displayWidth: Int,
    val displayHeight: Int,
    val viewLeft: Int,
    val viewTop: Int,
    val viewWidth: Int,
    val viewHeight: Int,
    val mirroredX: Boolean,
    val mirroredY: Boolean
)

/** Pure rules of `POST /v1/focus`, so JVM unit tests cover them. */
object FocusTapLogic {
    private const val MIN = 0f
    private const val MAX = 1f

    /** Throws [ApiException] with [ErrorCode.BAD_REQUEST] unless exactly one complete pair of values in [0, 1]. */
    fun target(request: FocusRequest): FocusTarget {
        val screen = pair(request.screenX, request.screenY)
        val snapshot = pair(request.snapshotX, request.snapshotY)
        return when {
            screen != null && snapshot == null && request.snapshotX == null && request.snapshotY == null ->
                FocusTarget.Screen(screen.first, screen.second)

            snapshot != null && screen == null && request.screenX == null && request.screenY == null ->
                FocusTarget.Snapshot(snapshot.first, snapshot.second)

            else -> throw ApiException(ErrorCode.BAD_REQUEST, Constants.Messages.FOCUS_NEEDS_ONE_PAIR)
        }
    }

    private fun pair(x: Float?, y: Float?): Pair<Float, Float>? {
        if (x == null || y == null) return null
        if (!inRange(x) || !inRange(y)) {
            throw ApiException(ErrorCode.BAD_REQUEST, Constants.Messages.FOCUS_OUT_OF_RANGE)
        }
        return x to y
    }

    private fun inRange(value: Float): Boolean = value.isFinite() && value in MIN..MAX

    /**
     * A normalized display point to a point in the preview view's own (unmirrored) coordinates, or null when the
     * point is outside the preview. A mirrored view shows the content at `width - x`, so the mirroring is undone.
     */
    fun screenToPreview(x: Float, y: Float, geometry: PreviewGeometry): PixelPoint? {
        val localX = x * geometry.displayWidth - geometry.viewLeft
        val localY = y * geometry.displayHeight - geometry.viewTop
        val inside = localX >= 0f && localY >= 0f && localX <= geometry.viewWidth && localY <= geometry.viewHeight
        if (!inside) return null
        return PixelPoint(
            x = if (geometry.mirroredX) geometry.viewWidth - localX else localX,
            y = if (geometry.mirroredY) geometry.viewHeight - localY else localY
        )
    }

    /**
     * A normalized point on the snapshot to a normalized point on the capture surface. The snapshot is the surface
     * turned clockwise by [rotationDegrees] (0, 90, 180, 270), so this turns it back.
     */
    fun snapshotToSurface(x: Float, y: Float, rotationDegrees: Int): Pair<Float, Float> =
        when (Math.floorMod(rotationDegrees, Constants.Orientation.DEGREES_PER_TURN)) {
            QUARTER_TURN -> y to MAX - x
            HALF_TURN -> MAX - x to MAX - y
            THREE_QUARTER_TURN -> MAX - y to x
            else -> x to y
        }

    private const val QUARTER_TURN = Constants.Orientation.BUCKET_DEGREES
    private const val HALF_TURN = 2 * Constants.Orientation.BUCKET_DEGREES
    private const val THREE_QUARTER_TURN = 3 * Constants.Orientation.BUCKET_DEGREES
}
