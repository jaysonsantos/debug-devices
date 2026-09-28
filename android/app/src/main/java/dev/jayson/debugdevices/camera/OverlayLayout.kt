package dev.jayson.debugdevices.camera

import kotlin.math.abs
import kotlin.math.cos
import kotlin.math.hypot
import kotlin.math.round
import kotlin.math.sin

// The highlight layout of `docs/overlay-layout.md` ("Geometry"), as one pure function: the Kotlin side of the shared
// test vectors `docs/overlay-layout-vectors.json`. The unit is a dp on the phone (an image pixel for the server).
// Text is not measured: sizes come from the character count.

/** The measured phone view: the system bar and cutout insets (screen pixels) and the status label band. */
data class PhoneFrameInput(val insets: PixelRect, val labelBand: Float)

/** A box in layout units: top-left `x`, `y`, size, an optional tag, and the label. */
data class LayoutBoxInput(
    val x: Float,
    val y: Float,
    val width: Float,
    val height: Float,
    val tag: String?,
    val label: String,
    /**
     * False for a box outside the phone view: it keeps its tag, colour, and legend row (the same as on the page), but
     * gets no drawn box and no badge, and the placement ignores it.
     */
    val visible: Boolean = true
)

/** An arrow: a direction in the layout frame (0 = right, 90 = down), an optional tag, and the label. */
data class LayoutArrowInput(val angleDeg: Float, val tag: String?, val label: String)

data class LayoutInput(
    val width: Float,
    val height: Float,
    val minBox: Float,
    val boxes: List<LayoutBoxInput>,
    val arrows: List<LayoutArrowInput>,
    /** True for the page and images; the phone has no inset. */
    val inset: Boolean
)

data class LaidBox(val tag: String, val colour: String, val label: String, val rect: PixelRect)

data class LaidBadge(
    val tag: String,
    val colour: String,
    val rect: PixelRect,
    /** True when no place next to the box was free: the badge is outside the cluster, with a leader line. */
    val outside: Boolean,
    /** From the badge centre to the nearest point of the drawn box. */
    val leader: Pair<PixelPoint, PixelPoint>?
)

data class LegendRow(val tag: String, val label: String, val colour: String)

/** [outside]: every corner overlaps a box. Images get a strip below; the phone draws it see-through at [rect]. */
data class LaidLegend(
    val rect: PixelRect,
    val rows: List<LegendRow>,
    val outside: Boolean,
    /** The first corner of the sorted list: where the phone draws an outside legend (at 50% opacity). */
    val firstCorner: PixelRect
)

data class LaidArrow(
    val tag: String,
    val colour: String,
    val label: String,
    val angleDeg: Float,
    val anchor: PixelPoint
)

data class LaidInset(val source: PixelRect, val dest: PixelRect, val scale: Float)

data class LayoutResult(
    val boxes: List<LaidBox>,
    val badges: List<LaidBadge>,
    val legend: LaidLegend?,
    val arrows: List<LaidArrow>,
    val inset: LaidInset?,
    /** The strip below an image for an outside legend (0 otherwise). */
    val extraHeight: Float
)

object OverlayLayout {
    /** Colour order: item `i` (boxes, then arrows) gets `COLOURS[i % 4]`. */
    val COLOURS = listOf("#00E676", "#00E5FF", "#FFEA00", "#FF4081")
    val GAPS = listOf(4f, 12f, 24f, 40f)
    const val CLEARANCE = 4f
    const val MARGIN = 8f
    const val BADGE_BASE = 8f
    const val BADGE_PER_CHAR = 9f
    const val BADGE_HEIGHT = 18f
    const val LEGEND_BASE = 12f
    const val LEGEND_PER_CHAR = 8f
    const val LEGEND_ROW = 18f
    const val FALLBACK_GAP = 16f
    const val ARROW_INSET = 28f
    const val INSET_THRESHOLD = 0.03f
    const val INSET_SCALE = 3f
    const val INSET_MAX_WIDTH = 0.25f
    const val OUTSIDE_STRIP_EXTRA = 2 * MARGIN
    const val MAX_TAG_LENGTH = 3
    private const val HALF = 2f
    private const val ROUND_FACTOR = 100f
    private const val FIRST_LETTER = 'A'
    private const val LAST_LETTER = 'Z'
    private const val TAG_SEPARATOR = ": "

    /** Given tags (first 3 characters) stay; the others get the first free letter, boxes then arrows. */
    fun tags(given: List<String?>): List<String> {
        val used = given.filterNotNull().map { it.take(MAX_TAG_LENGTH) }.toMutableSet()
        return given.map { tag ->
            tag?.take(MAX_TAG_LENGTH) ?: (
                (FIRST_LETTER..LAST_LETTER).map { it.toString() }.firstOrNull { it !in used }
                    ?: LAST_LETTER.toString()
                ).also { used += it }
        }
    }

    fun colour(index: Int): String = COLOURS[index % COLOURS.size]

    /** ARGB of a `#RRGGBB` colour, for drawing. */
    fun argb(colour: String): Int = (0xFF000000L or colour.removePrefix("#").toLong(HEX_RADIX)).toInt()

    private const val HEX_RADIX = 16

    fun grow(rect: PixelRect, min: Float): PixelRect {
        val width = maxOf(rect.right - rect.left, min)
        val height = maxOf(rect.bottom - rect.top, min)
        val cx = (rect.left + rect.right) / HALF
        val cy = (rect.top + rect.bottom) / HALF
        return PixelRect(cx - width / HALF, cy - height / HALF, cx + width / HALF, cy + height / HALF)
    }

    fun badgeSize(tag: String): PixelPoint = PixelPoint(BADGE_BASE + BADGE_PER_CHAR * tag.length, BADGE_HEIGHT)

    fun layout(input: LayoutInput): LayoutResult {
        val tags = tags(input.boxes.map { it.tag } + input.arrows.map { it.tag })
        // Tags, colours, and legend rows come from the full list; the placement uses the visible boxes only.
        val visible = input.boxes.indices.filter { input.boxes[it].visible }
        val given = visible.map { i -> input.boxes[i].let { PixelRect(it.x, it.y, it.x + it.width, it.y + it.height) } }
        val drawn = given.map { grow(it, input.minBox) }
        val boxes = visible.mapIndexed { j, i -> LaidBox(tags[i], colour(i), input.boxes[i].label, drawn[j]) }
        val rows = input.boxes.mapIndexed { i, box -> LegendRow(tags[i], box.label, colour(i)) } +
            input.arrows.mapIndexed { i, arrow ->
                val index = input.boxes.size + i
                LegendRow(tags[index], arrow.label, colour(index))
            }
        val legend = if (rows.isEmpty()) null else placeLegend(input.width, input.height, drawn, rows)
        val legendInside = legend?.takeIf { !it.outside }?.rect
        val badges = mutableListOf<LaidBadge>()
        drawn.forEachIndexed { j, rect ->
            val i = visible[j]
            badges +=
                placeBadge(
                    rect,
                    badgeSize(tags[i]),
                    input.width,
                    input.height,
                    drawn,
                    badges.map {
                        it.rect
                    },
                    legendInside
                )
                    .copy(tag = tags[i], colour = colour(i))
        }
        val arrows = input.arrows.mapIndexed { i, arrow ->
            val index = input.boxes.size + i
            LaidArrow(
                tags[index],
                colour(index),
                arrow.label,
                arrow.angleDeg,
                arrowAnchor(arrow.angleDeg, input.width, input.height)
            )
        }
        val inset = if (input.inset) {
            placeInset(
                input.width,
                input.height,
                given,
                drawn,
                badges.map {
                    it.rect
                },
                legendInside
            )
        } else {
            null
        }
        val extraHeight = if (legend?.outside ==
            true
        ) {
            legend.rect.bottom - legend.rect.top + OUTSIDE_STRIP_EXTRA
        } else {
            0f
        }
        return LayoutResult(boxes, badges, legend, arrows, inset, extraHeight).rounded()
    }

    fun legendSize(rows: List<LegendRow>): PixelPoint {
        val longest = rows.maxOf { (it.tag + TAG_SEPARATOR + it.label).length }
        return PixelPoint(LEGEND_BASE + LEGEND_PER_CHAR * longest, LEGEND_BASE + LEGEND_ROW * rows.size)
    }

    fun placeLegend(width: Float, height: Float, drawn: List<PixelRect>, rows: List<LegendRow>): LaidLegend {
        val size = legendSize(rows)
        val corners = cornerRects(width, height, size)
        if (drawn.isEmpty()) return LaidLegend(corners.first(), rows, outside = false, firstCorner = corners.first())
        val grown = drawn.map { inflate(it, CLEARANCE) }
        // sortedByDescending is stable: equal distances keep the corner order.
        val order = corners.sortedByDescending { corner -> drawn.minOf { distance(corner, it) } }
        val free = order.firstOrNull { corner -> grown.none { overlaps(corner, it) } }
        return if (free != null) {
            LaidLegend(free, rows, outside = false, firstCorner = order.first())
        } else {
            LaidLegend(
                PixelRect(MARGIN, height + MARGIN, MARGIN + size.x, height + MARGIN + size.y),
                rows,
                outside = true,
                firstCorner = order.first()
            )
        }
    }

    /** Rectangles of [size], 8 units in from the corners: top-left, top-right, bottom-left, bottom-right. */
    fun cornerRects(width: Float, height: Float, size: PixelPoint): List<PixelRect> = listOf(
        PixelRect(MARGIN, MARGIN, MARGIN + size.x, MARGIN + size.y),
        PixelRect(width - MARGIN - size.x, MARGIN, width - MARGIN, MARGIN + size.y),
        PixelRect(MARGIN, height - MARGIN - size.y, MARGIN + size.x, height - MARGIN),
        PixelRect(width - MARGIN - size.x, height - MARGIN - size.y, width - MARGIN, height - MARGIN)
    )

    /** The 8 positions in order: right, left, above, below, above-right, above-left, below-right, below-left. */
    fun badgeCandidates(box: PixelRect, size: PixelPoint, gap: Float): List<PixelRect> {
        val w = size.x
        val h = size.y
        val cx = (box.left + box.right) / HALF
        val cy = (box.top + box.bottom) / HALF
        val corners = listOf(
            PixelPoint(box.right + gap, cy - h / HALF),
            PixelPoint(box.left - gap - w, cy - h / HALF),
            PixelPoint(cx - w / HALF, box.top - gap - h),
            PixelPoint(cx - w / HALF, box.bottom + gap),
            PixelPoint(box.right + gap, box.top - gap - h),
            PixelPoint(box.left - gap - w, box.top - gap - h),
            PixelPoint(box.right + gap, box.bottom + gap),
            PixelPoint(box.left - gap - w, box.bottom + gap)
        )
        return corners.map { PixelRect(it.x, it.y, it.x + w, it.y + h) }
    }

    fun placeBadge(
        box: PixelRect,
        size: PixelPoint,
        width: Float,
        height: Float,
        drawn: List<PixelRect>,
        badges: List<PixelRect>,
        legend: PixelRect?
    ): LaidBadge {
        val blocked =
            drawn.map { inflate(it, CLEARANCE) } + badges.map { inflate(it, CLEARANCE) } + listOfNotNull(legend)
        for (gap in GAPS) {
            for (candidate in badgeCandidates(box, size, gap)) {
                if (inside(candidate, width, height) && blocked.none { overlaps(candidate, it) }) {
                    return LaidBadge("", "", candidate, outside = false, leader = null)
                }
            }
        }
        val badge = outsideCluster(box, size, width, height, drawn)
        val centre = PixelPoint((badge.left + badge.right) / HALF, (badge.top + badge.bottom) / HALF)
        return LaidBadge("", "", badge, outside = true, leader = centre to nearestPoint(centre, box))
    }

    private fun outsideCluster(
        box: PixelRect,
        size: PixelPoint,
        width: Float,
        height: Float,
        drawn: List<PixelRect>
    ): PixelRect {
        val cluster =
            PixelRect(
                drawn.minOf {
                    it.left
                },
                drawn.minOf { it.top },
                drawn.maxOf { it.right },
                drawn.maxOf { it.bottom }
            )
        val cx = (cluster.left + cluster.right) / HALF
        val cy = (cluster.top + cluster.bottom) / HALF
        var dx = (box.left + box.right) / HALF - cx
        var dy = (box.top + box.bottom) / HALF - cy
        val length = hypot(dx, dy)
        if (length == 0f) {
            dx = 1f
            dy = 0f
        } else {
            dx /= length
            dy /= length
        }
        val leave = rayExit(cx, cy, dx, dy, cluster)
        val distance = FALLBACK_GAP + maxOf(size.x, size.y) / HALF
        val bx = leave.x + dx * distance
        val by = leave.y + dy * distance
        val left = (bx - size.x / HALF).coerceIn(0f, width - size.x)
        val top = (by - size.y / HALF).coerceIn(0f, height - size.y)
        return PixelRect(left, top, left + size.x, top + size.y)
    }

    /** Where a ray from ([cx], [cy]) inside [rect] in direction ([dx], [dy]) leaves it. */
    private fun rayExit(cx: Float, cy: Float, dx: Float, dy: Float, rect: PixelRect): PixelPoint {
        val tx = when {
            dx > 0f -> (rect.right - cx) / dx
            dx < 0f -> (rect.left - cx) / dx
            else -> Float.POSITIVE_INFINITY
        }
        val ty = when {
            dy > 0f -> (rect.bottom - cy) / dy
            dy < 0f -> (rect.top - cy) / dy
            else -> Float.POSITIVE_INFINITY
        }
        val t = minOf(tx, ty)
        return PixelPoint(cx + dx * t, cy + dy * t)
    }

    /** On the ray from the view centre at [angleDeg], 28 units inside the view edge. */
    fun arrowAnchor(angleDeg: Float, width: Float, height: Float): PixelPoint {
        val radians = Math.toRadians(angleDeg.toDouble())
        val dx = cos(radians).toFloat().let { if (abs(it) < EPSILON) 0f else it }
        val dy = sin(radians).toFloat().let { if (abs(it) < EPSILON) 0f else it }
        val inner = PixelRect(ARROW_INSET, ARROW_INSET, width - ARROW_INSET, height - ARROW_INSET)
        return rayExit(width / HALF, height / HALF, dx, dy, inner)
    }

    private const val EPSILON = 1e-6f

    fun placeInset(
        width: Float,
        height: Float,
        given: List<PixelRect>,
        drawn: List<PixelRect>,
        badges: List<PixelRect>,
        legend: PixelRect?
    ): LaidInset? {
        if (given.isEmpty()) return null
        val largest = given.maxOf { maxOf(it.right - it.left, it.bottom - it.top) }
        if (largest >= INSET_THRESHOLD * width) return null
        val bounds =
            PixelRect(
                given.minOf {
                    it.left
                },
                given.minOf { it.top },
                given.maxOf { it.right },
                given.maxOf { it.bottom }
            )
        val grow = maxOf(bounds.right - bounds.left, bounds.bottom - bounds.top) / HALF
        val source = PixelRect(
            maxOf(0f, bounds.left - grow),
            maxOf(0f, bounds.top - grow),
            minOf(width, bounds.right + grow),
            minOf(height, bounds.bottom + grow)
        )
        val sourceWidth = source.right - source.left
        val scale = minOf(INSET_SCALE, INSET_MAX_WIDTH * width / sourceWidth)
        val size = PixelPoint(sourceWidth * scale, (source.bottom - source.top) * scale)
        val blocked = drawn.map { inflate(it, CLEARANCE) } + badges + listOfNotNull(legend)
        val dest =
            cornerRects(width, height, size).firstOrNull { corner -> blocked.none { overlaps(corner, it) } }
                ?: return null
        return LaidInset(source, dest, scale)
    }

    /** The point of [rect] nearest to [point] (the point itself when it is inside). */
    fun nearestPoint(point: PixelPoint, rect: PixelRect): PixelPoint =
        PixelPoint(point.x.coerceIn(rect.left, rect.right), point.y.coerceIn(rect.top, rect.bottom))

    /** More than 0 of overlap in both axes (touching edges do not overlap). */
    fun overlaps(a: PixelRect, b: PixelRect): Boolean =
        minOf(a.right, b.right) - maxOf(a.left, b.left) > 0f && minOf(a.bottom, b.bottom) - maxOf(a.top, b.top) > 0f

    fun inside(rect: PixelRect, width: Float, height: Float): Boolean =
        rect.left >= 0f && rect.top >= 0f && rect.right <= width && rect.bottom <= height

    private fun inflate(rect: PixelRect, by: Float) = PixelRect(
        rect.left - by,
        rect.top - by,
        rect.right + by,
        rect.bottom + by
    )

    /** 0 when they overlap or touch, else the distance between the nearest points. */
    fun distance(a: PixelRect, b: PixelRect): Float {
        val dx = maxOf(0f, b.left - a.right, a.left - b.right)
        val dy = maxOf(0f, b.top - a.bottom, a.top - b.bottom)
        return hypot(dx, dy)
    }

    private fun r(value: Float): Float = round(value * ROUND_FACTOR) / ROUND_FACTOR

    private fun PixelRect.rounded() = PixelRect(r(left), r(top), r(left) + r(right - left), r(top) + r(bottom - top))

    private fun PixelPoint.rounded() = PixelPoint(r(x), r(y))

    private fun LayoutResult.rounded() = LayoutResult(
        boxes = boxes.map { it.copy(rect = it.rect.rounded()) },
        badges = badges.map { b ->
            b.copy(
                rect = b.rect.rounded(),
                leader = b.leader?.let {
                    it.first.rounded() to
                        it.second.rounded()
                }
            )
        },
        legend = legend?.copy(rect = legend.rect.rounded(), firstCorner = legend.firstCorner.rounded()),
        arrows = arrows.map { it.copy(anchor = it.anchor.rounded()) },
        inset = inset?.let { LaidInset(it.source.rounded(), it.dest.rounded(), r(it.scale)) },
        extraHeight = r(extraHeight)
    )

    /**
     * A screen rectangle in the viewer's frame, for a viewer who sees the screen turned by [viewerDegrees]
     * (the frame is [screenHeight] wide when the viewer is sideways). The phone runs the layout in this frame, so
     * tags and the legend read upright.
     */
    fun toViewer(rect: PixelRect, viewerDegrees: Int, screenWidth: Float, screenHeight: Float): PixelRect {
        val a = pointToViewer(PixelPoint(rect.left, rect.top), viewerDegrees, screenWidth, screenHeight)
        val b = pointToViewer(PixelPoint(rect.right, rect.bottom), viewerDegrees, screenWidth, screenHeight)
        return PixelRect(minOf(a.x, b.x), minOf(a.y, b.y), maxOf(a.x, b.x), maxOf(a.y, b.y))
    }

    /**
     * The phone view (`docs/overlay-layout.md`, "The phone view"): the screen without the system bars and cutouts
     * ([insets], in screen pixels), in the viewer's frame, without the band at the viewer's top that holds the app's
     * status label ([labelBand], viewer-frame pixels from the safe top; the label turns with the viewer).
     */
    fun phoneFrame(
        screenWidth: Float,
        screenHeight: Float,
        insets: PixelRect,
        viewerDegrees: Int,
        labelBand: Float
    ): PixelRect {
        // Insets that use all of the screen give an empty (not an inverted) safe rectangle.
        val right = maxOf(insets.left, screenWidth - insets.right)
        val bottom = maxOf(insets.top, screenHeight - insets.bottom)
        val safe = PixelRect(insets.left, insets.top, right, bottom)
        val viewer = toViewer(safe, viewerDegrees, screenWidth, screenHeight)
        return viewer.copy(top = minOf(viewer.top + labelBand, viewer.bottom))
    }

    /**
     * The status label band for the phone view, or null while it is not measured for the current orientation:
     * the window has no size yet (N8), or the turned overlay container is still laid out for the old orientation
     * ([laidOutWidth] is not the viewer-frame width of the safe area for [viewerDegrees], N10). All values in pixels.
     */
    fun labelBand(
        windowWidth: Int,
        windowHeight: Int,
        laidOutWidth: Int,
        safeWidth: Int,
        safeHeight: Int,
        viewerDegrees: Int,
        labelBottom: Int,
        gap: Float
    ): Float? {
        // The window has no size: not measured (null). The insets use all of it: measured, no room (an empty frame).
        if (windowWidth <= 0 || windowHeight <= 0) return null
        if (safeWidth <= 0 || safeHeight <= 0) return 0f
        val sideways = Math.floorMod(viewerDegrees, 2 * Constants.Orientation.BUCKET_DEGREES) != 0
        val expectedWidth = if (sideways) safeHeight else safeWidth
        if (laidOutWidth != expectedWidth) return null
        return labelBottom + gap
    }

    /** The inverse of [toViewer]: a viewer-frame rectangle back in screen pixels. */
    fun fromViewer(rect: PixelRect, viewerDegrees: Int, screenWidth: Float, screenHeight: Float): PixelRect {
        // Turning back is the same map with the opposite turn and the frame size swapped when sideways.
        val sideways = Math.floorMod(viewerDegrees, 2 * Constants.Orientation.BUCKET_DEGREES) != 0
        val frameWidth = if (sideways) screenHeight else screenWidth
        val frameHeight = if (sideways) screenWidth else screenHeight
        return toViewer(rect, -viewerDegrees, frameWidth, frameHeight)
    }

    /** True when some part of [rect] is inside [frame] (touching does not count). */
    fun inFrame(rect: PixelRect, frame: PixelRect): Boolean = overlaps(rect, frame)

    /** The inverse of the canvas turn in `OverlayView`: `translate` to the viewer's top-left corner, then `rotate`. */
    fun pointToViewer(point: PixelPoint, viewerDegrees: Int, screenWidth: Float, screenHeight: Float): PixelPoint =
        when (Math.floorMod(viewerDegrees, Constants.Orientation.DEGREES_PER_TURN)) {
            Constants.Orientation.BUCKET_DEGREES -> PixelPoint(point.y, screenWidth - point.x)
            2 * Constants.Orientation.BUCKET_DEGREES -> PixelPoint(screenWidth - point.x, screenHeight - point.y)
            3 * Constants.Orientation.BUCKET_DEGREES -> PixelPoint(screenHeight - point.y, point.x)
            else -> point
        }
}
