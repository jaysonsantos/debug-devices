package dev.jayson.debugdevices.camera

import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.util.AttributeSet
import android.util.TypedValue
import android.view.View

/**
 * Draws the `POST /v1/overlay` boxes above the preview. It sits next to the preview view, not inside it, so it is
 * never mirrored and the labels stay readable. [boxes] gives the boxes in this view's pixels.
 */
class OverlayView(context: Context, attrs: AttributeSet? = null) : View(context, attrs) {
    var boxes: (width: Float, height: Float) -> List<Pair<PixelRect, String>> = { _, _ -> emptyList() }

    private val border = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        color = Color.GREEN
        strokeWidth = dp(BORDER_DP)
    }
    private val text = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.WHITE
        textSize = TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_SP, LABEL_SP, resources.displayMetrics)
    }
    private val labelBackground = Paint().apply { color = LABEL_BACKGROUND }
    private val padding = dp(LABEL_PADDING_DP)

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        for ((rect, label) in boxes(width.toFloat(), height.toFloat())) {
            canvas.drawRect(rect.left, rect.top, rect.right, rect.bottom, border)
            if (label.isNotEmpty()) drawLabel(canvas, rect, label)
        }
    }

    /** The label sits above the box, or inside its top edge when there is no room above. */
    private fun drawLabel(canvas: Canvas, rect: PixelRect, label: String) {
        val textHeight = text.fontMetrics.let { it.descent - it.ascent }
        val boxHeight = textHeight + 2 * padding
        val top = if (rect.top - boxHeight >= 0f) rect.top - boxHeight else rect.top
        val left = rect.left.coerceAtLeast(0f)
        canvas.drawRect(left, top, left + text.measureText(label) + 2 * padding, top + boxHeight, labelBackground)
        canvas.drawText(label, left + padding, top + padding - text.fontMetrics.ascent, text)
    }

    private fun dp(value: Float): Float =
        TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, value, resources.displayMetrics)

    private companion object {
        const val BORDER_DP = 3f
        const val LABEL_SP = 12f
        const val LABEL_PADDING_DP = 3f
        val LABEL_BACKGROUND = Color.argb(0xCC, 0, 0, 0)
    }
}
