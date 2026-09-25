package dev.jayson.debugdevices.camera

import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.util.AttributeSet
import android.util.TypedValue
import android.view.View
import kotlin.math.atan2
import kotlin.math.cos
import kotlin.math.sin

/**
 * Draws the `POST /v1/overlay` boxes and arrows above the preview. It sits next to the preview view, not inside it,
 * so it is never mirrored. Labels are turned upright for the viewer. [scene] gives everything in this view's pixels.
 */
class OverlayView(context: Context, attrs: AttributeSet? = null) : View(context, attrs) {
    var scene: (width: Float, height: Float, arrowInset: Float, arrowLength: Float) -> OverlayScene =
        { _, _, _, _ -> OverlayScene.EMPTY }

    private val stroke = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        color = Color.GREEN
        strokeWidth = dp(BORDER_DP)
        strokeCap = Paint.Cap.ROUND
    }
    private val text = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.WHITE
        textSize = TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_SP, LABEL_SP, resources.displayMetrics)
    }
    private val labelBackground = Paint().apply { color = LABEL_BACKGROUND }
    private val padding = dp(LABEL_PADDING_DP)

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        val current = scene(width.toFloat(), height.toFloat(), dp(ARROW_INSET_DP), dp(ARROW_LENGTH_DP))
        for ((rect, label) in current.boxes) {
            canvas.drawRect(rect.left, rect.top, rect.right, rect.bottom, stroke)
            drawLabel(canvas, OverlayLogic.viewerTopLeft(rect, current.viewerDegrees), label, current.viewerDegrees)
        }
        for ((arrow, label) in current.arrows) {
            drawArrow(canvas, arrow)
            drawLabel(canvas, arrow.tail, label, current.viewerDegrees)
        }
    }

    private fun drawArrow(canvas: Canvas, arrow: ViewArrow) {
        canvas.drawLine(arrow.tail.x, arrow.tail.y, arrow.tip.x, arrow.tip.y, stroke)
        val back = atan2(arrow.tail.y - arrow.tip.y, arrow.tail.x - arrow.tip.x)
        val head = dp(ARROW_HEAD_DP)
        for (side in listOf(-ARROW_HEAD_ANGLE, ARROW_HEAD_ANGLE)) {
            val angle = back + side
            canvas.drawLine(
                arrow.tip.x,
                arrow.tip.y,
                arrow.tip.x + head * cos(angle),
                arrow.tip.y + head * sin(angle),
                stroke
            )
        }
    }

    /** The label sits above [anchor] for the viewer, turned upright, and moved inside the view when needed. */
    private fun drawLabel(canvas: Canvas, anchor: PixelPoint, label: String, viewerDegrees: Int) {
        if (label.isEmpty()) return
        val labelWidth = text.measureText(label) + 2 * padding
        val labelHeight = text.fontMetrics.let { it.descent - it.ascent } + 2 * padding
        val onScreen = OverlayLogic.labelRect(anchor, labelWidth, labelHeight, viewerDegrees)
        val shift = OverlayLogic.shiftInside(onScreen, width.toFloat(), height.toFloat())
        canvas.save()
        canvas.translate(anchor.x + shift.x, anchor.y + shift.y)
        canvas.rotate(viewerDegrees.toFloat())
        canvas.drawRect(0f, -labelHeight, labelWidth, 0f, labelBackground)
        canvas.drawText(label, padding, -labelHeight + padding - text.fontMetrics.ascent, text)
        canvas.restore()
    }

    private fun dp(value: Float): Float =
        TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, value, resources.displayMetrics)

    private companion object {
        const val BORDER_DP = 3f
        const val LABEL_SP = 12f
        const val LABEL_PADDING_DP = 3f
        const val ARROW_INSET_DP = 12f
        const val ARROW_LENGTH_DP = 56f
        const val ARROW_HEAD_DP = 14f
        const val ARROW_HEAD_ANGLE = 0.5f
        val LABEL_BACKGROUND = Color.argb(0xCC, 0, 0, 0)
    }
}
