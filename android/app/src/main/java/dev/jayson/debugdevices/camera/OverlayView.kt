package dev.jayson.debugdevices.camera

import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.Typeface
import android.util.AttributeSet
import android.view.View
import kotlin.math.atan2
import kotlin.math.cos
import kotlin.math.sin

/**
 * Draws the `POST /v1/overlay` boxes and arrows above the preview by `docs/overlay-layout.md`: tags in badges, a legend
 * with the labels, a dark outline under a coloured one. It sits next to the preview view, so it is never mirrored.
 * The layout ([OverlayLayout]) runs in dp in the viewer's frame, so the text reads upright when the phone is sideways.
 * [scene] gives the boxes and arrows in this view's pixels.
 */
class OverlayView(context: Context, attrs: AttributeSet? = null) : View(context, attrs) {
    var scene: (width: Float, height: Float, arrowInset: Float, arrowLength: Float) -> OverlayScene =
        { _, _, _, _ -> OverlayScene.EMPTY }

    /** The system bar and cutout insets of this view, in screen pixels (left, top, right, bottom as a rect). */
    var insets: () -> PixelRect = { NO_INSETS }

    /** The height of the app's status label band at the viewer's top, in pixels (the label turns with the viewer). */
    var labelBand: () -> Float = { 0f }

    private val density = resources.displayMetrics.density

    // All sizes below are dp: the canvas is scaled by the density before drawing.
    private val colourStroke = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeWidth = OUTLINE_DP
        strokeCap = Paint.Cap.ROUND
    }
    private val darkStroke = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        color = DARK_OUTLINE
        strokeWidth = OUTLINE_DP + 2 * DARK_EXTRA_DP
        strokeCap = Paint.Cap.ROUND
    }
    private val leaderStroke = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeWidth = LEADER_DP
    }
    private val fill = Paint(Paint.ANTI_ALIAS_FLAG)

    /** Monospace, so the text fits the sizes that the layout takes from the character count. */
    private val text = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        typeface = Typeface.MONOSPACE
        textSize = TEXT_DP
    }

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        val screenWidth = width.toFloat()
        val screenHeight = height.toFloat()
        val current = scene(screenWidth, screenHeight, ARROW_INSET_PX, ARROW_LENGTH_PX)
        if (current.boxes.isEmpty() && current.arrows.isEmpty()) return
        val degrees = current.viewerDegrees
        // The phone view: the safe area without the status label, in the viewer's frame (screen pixels).
        val frame = OverlayLayout.phoneFrame(screenWidth, screenHeight, insets(), degrees, labelBand())
        val frameWidth = (frame.right - frame.left) / density
        val frameHeight = (frame.bottom - frame.top) / density
        val boxes = current.boxes.map { item ->
            // A box outside the phone view keeps its place (tag, colour, legend row) but is not drawn.
            val r = item.rect?.let { OverlayLayout.toViewer(it, degrees, screenWidth, screenHeight) }
            val shown = r != null && OverlayLayout.inFrame(r, frame)
            val rect = r ?: HIDDEN
            LayoutBoxInput(
                (rect.left - frame.left) / density,
                (rect.top - frame.top) / density,
                (rect.right - rect.left) / density,
                (rect.bottom - rect.top) / density,
                item.tag,
                item.label,
                visible = shown
            )
        }
        val arrows = current.arrows.map { (arrow, tag, label) ->
            val tip = OverlayLayout.pointToViewer(arrow.tip, degrees, screenWidth, screenHeight)
            val tail = OverlayLayout.pointToViewer(arrow.tail, degrees, screenWidth, screenHeight)
            val angle = Math.toDegrees(atan2((tip.y - tail.y).toDouble(), (tip.x - tail.x).toDouble())).toFloat()
            LayoutArrowInput(angle, tag, label)
        }
        val layout = OverlayLayout.layout(
            LayoutInput(frameWidth, frameHeight, MIN_BOX_DP, boxes, arrows, inset = false)
        )

        canvas.save()
        turnToViewer(canvas, degrees, screenWidth, screenHeight)
        canvas.translate(frame.left, frame.top)
        canvas.scale(density, density)
        layout.boxes.forEachIndexed { i, box -> drawBox(canvas, box, layout.badges[i]) }
        layout.arrows.forEach { drawArrow(canvas, it) }
        layout.legend?.let { drawLegend(canvas, it) }
        canvas.restore()
    }

    /** Maps the viewer frame onto the screen: the inverse of [OverlayLayout.pointToViewer]. */
    private fun turnToViewer(canvas: Canvas, degrees: Int, screenWidth: Float, screenHeight: Float) {
        when (Math.floorMod(degrees, Constants.Orientation.DEGREES_PER_TURN)) {
            Constants.Orientation.BUCKET_DEGREES -> canvas.translate(screenWidth, 0f)
            2 * Constants.Orientation.BUCKET_DEGREES -> canvas.translate(screenWidth, screenHeight)
            3 * Constants.Orientation.BUCKET_DEGREES -> canvas.translate(0f, screenHeight)
        }
        canvas.rotate(degrees.toFloat())
    }

    private fun drawBox(canvas: Canvas, box: LaidBox, badge: LaidBadge) {
        val colour = OverlayLayout.argb(box.colour)
        val r = box.rect
        canvas.drawRect(r.left, r.top, r.right, r.bottom, darkStroke)
        colourStroke.color = colour
        canvas.drawRect(r.left, r.top, r.right, r.bottom, colourStroke)
        badge.leader?.let { (from, to) ->
            leaderStroke.color = colour
            canvas.drawLine(from.x, from.y, to.x, to.y, leaderStroke)
        }
        drawBadge(canvas, badge.rect, badge.tag, colour)
    }

    private fun drawBadge(canvas: Canvas, rect: PixelRect, tag: String, colour: Int) {
        fill.color = colour
        canvas.drawRect(rect.left, rect.top, rect.right, rect.bottom, fill)
        text.color = BADGE_TEXT
        text.alpha = OPAQUE
        canvas.drawText(tag, rect.left + TEXT_INSET_DP, baseline(rect.top, rect.bottom), text)
    }

    /** The arrow ends at its anchor and points out of the view; its tag badge sits at the tail. */
    private fun drawArrow(canvas: Canvas, arrow: LaidArrow) {
        val colour = OverlayLayout.argb(arrow.colour)
        val radians = Math.toRadians(arrow.angleDeg.toDouble())
        val dx = cos(radians).toFloat()
        val dy = sin(radians).toFloat()
        val tip = arrow.anchor
        val tail = PixelPoint(tip.x - dx * ARROW_LENGTH_DP, tip.y - dy * ARROW_LENGTH_DP)
        colourStroke.color = colour
        for (paint in listOf(darkStroke, colourStroke)) {
            canvas.drawLine(tail.x, tail.y, tip.x, tip.y, paint)
            val back = atan2(tail.y - tip.y, tail.x - tip.x)
            for (side in listOf(-ARROW_HEAD_ANGLE, ARROW_HEAD_ANGLE)) {
                val angle = back + side
                canvas.drawLine(
                    tip.x,
                    tip.y,
                    tip.x + ARROW_HEAD_DP * cos(angle),
                    tip.y + ARROW_HEAD_DP * sin(angle),
                    paint
                )
            }
        }
        val size = OverlayLayout.badgeSize(arrow.tag)
        drawBadge(
            canvas,
            PixelRect(tail.x - size.x / 2, tail.y - size.y / 2, tail.x + size.x / 2, tail.y + size.y / 2),
            arrow.tag,
            colour
        )
    }

    /**
     * Dark 85% panel with `tag: label` rows in the item colours. An outside legend (every corner covers a box) has
     * no room below the phone screen: the phone draws it in the first corner of the sorted list, the whole legend at
     * 50% opacity (`docs/overlay-layout.md`, legend corner).
     */
    private fun drawLegend(canvas: Canvas, legend: LaidLegend) {
        val rect = if (legend.outside) legend.firstCorner else legend.rect
        if (legend.outside) canvas.saveLayerAlpha(rect.left, rect.top, rect.right, rect.bottom, OUTSIDE_LEGEND_ALPHA)
        fill.color = LEGEND_BACKGROUND
        canvas.drawRect(rect.left, rect.top, rect.right, rect.bottom, fill)
        legend.rows.forEachIndexed { i, row ->
            text.color = OverlayLayout.argb(row.colour)
            val top = rect.top + OverlayLayout.LEGEND_BASE / 2 + i * OverlayLayout.LEGEND_ROW
            canvas.drawText(
                "${row.tag}: ${row.label}",
                rect.left + OverlayLayout.LEGEND_BASE / 2,
                baseline(
                    top,
                    top + OverlayLayout.LEGEND_ROW
                ),
                text
            )
        }
        if (legend.outside) canvas.restore()
    }

    /** The text baseline that centres a line vertically between [top] and [bottom]. */
    private fun baseline(top: Float, bottom: Float): Float {
        val metrics = text.fontMetrics
        return (top + bottom) / 2 - (metrics.ascent + metrics.descent) / 2
    }

    private companion object {
        const val OUTLINE_DP = 3f
        const val DARK_EXTRA_DP = 2f
        const val LEADER_DP = 1.5f
        const val MIN_BOX_DP = 24f

        /** Monospace at 13 dp: about 7.8 dp per character, inside the 8 (legend) and 9 (badge) of the layout. */
        const val TEXT_DP = 13f
        const val TEXT_INSET_DP = 4f
        const val ARROW_LENGTH_DP = 40f
        const val ARROW_HEAD_DP = 10f
        const val ARROW_HEAD_ANGLE = 0.5f

        /** Only the direction of the scene arrows counts here; the layout places them. */
        const val ARROW_INSET_PX = 0f
        const val ARROW_LENGTH_PX = 1f
        const val OPAQUE = 0xFF

        /** 50% of 255: the opacity of an outside legend on the phone. */
        const val OUTSIDE_LEGEND_ALPHA = 0x80
        val DARK_OUTLINE = Color.argb(0x99, 0, 0, 0)
        val LEGEND_BACKGROUND = Color.argb(0xD9, 0, 0, 0)

        /** A rectangle for a hidden box: only its tag and colour count, the layout does not place it. */
        val HIDDEN = PixelRect(0f, 0f, 0f, 0f)
        val NO_INSETS = PixelRect(0f, 0f, 0f, 0f)
        val BADGE_TEXT = Color.BLACK
    }
}
