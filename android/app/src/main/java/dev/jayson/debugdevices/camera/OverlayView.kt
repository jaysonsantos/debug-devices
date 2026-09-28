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
        val sideways = Math.floorMod(degrees, 2 * Constants.Orientation.BUCKET_DEGREES) != 0
        val frameWidth = (if (sideways) screenHeight else screenWidth) / density
        val frameHeight = (if (sideways) screenWidth else screenHeight) / density
        val boxes = current.boxes.map { item ->
            val r = OverlayLayout.toViewer(item.rect, degrees, screenWidth, screenHeight)
            LayoutBoxInput(
                r.left / density,
                r.top / density,
                (r.right - r.left) / density,
                (r.bottom - r.top) / density,
                item.tag,
                item.label
            )
        }
        val arrows = current.arrows.map { (arrow, label) ->
            val tip = OverlayLayout.pointToViewer(arrow.tip, degrees, screenWidth, screenHeight)
            val tail = OverlayLayout.pointToViewer(arrow.tail, degrees, screenWidth, screenHeight)
            val angle = Math.toDegrees(atan2((tip.y - tail.y).toDouble(), (tip.x - tail.x).toDouble())).toFloat()
            LayoutArrowInput(angle, null, label)
        }
        val layout = OverlayLayout.layout(
            LayoutInput(frameWidth, frameHeight, MIN_BOX_DP, boxes, arrows, inset = false)
        )

        canvas.save()
        turnToViewer(canvas, degrees, screenWidth, screenHeight)
        canvas.scale(density, density)
        layout.boxes.forEachIndexed { i, box -> drawBox(canvas, box, layout.badges[i]) }
        layout.arrows.forEach { drawArrow(canvas, it) }
        layout.legend?.let { drawLegend(canvas, it, frameWidth, frameHeight) }
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
     * no room below the phone screen, so it is drawn see-through in the first corner (rule 2, phone).
     */
    private fun drawLegend(canvas: Canvas, legend: LaidLegend, frameWidth: Float, frameHeight: Float) {
        val size = PixelPoint(legend.rect.right - legend.rect.left, legend.rect.bottom - legend.rect.top)
        val rect = if (legend.outside) OverlayLayout.cornerRects(frameWidth, frameHeight, size).first() else legend.rect
        fill.color = if (legend.outside) LEGEND_SEE_THROUGH else LEGEND_BACKGROUND
        canvas.drawRect(rect.left, rect.top, rect.right, rect.bottom, fill)
        legend.rows.forEachIndexed { i, row ->
            text.color = OverlayLayout.argb(row.colour)
            text.alpha = if (legend.outside) SEE_THROUGH_TEXT_ALPHA else OPAQUE
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
        text.alpha = OPAQUE
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
        const val SEE_THROUGH_TEXT_ALPHA = 0x99
        val DARK_OUTLINE = Color.argb(0x99, 0, 0, 0)
        val LEGEND_BACKGROUND = Color.argb(0xD9, 0, 0, 0)
        val LEGEND_SEE_THROUGH = Color.argb(0x66, 0, 0, 0)
        val BADGE_TEXT = Color.BLACK
    }
}
