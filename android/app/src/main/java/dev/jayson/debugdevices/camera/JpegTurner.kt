package dev.jayson.debugdevices.camera

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Matrix
import androidx.exifinterface.media.ExifInterface
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream

/**
 * Makes a still JPEG upright in its pixels (C13 of the contract). A JPEG with EXIF `Orientation` 1 or none comes back
 * unchanged. Else the app decodes it, turns (and mirrors) the pixels by [ExifTurnLogic], and encodes it again without
 * an EXIF block, so `Orientation` is absent. Slow and memory-heavy (two full bitmaps): call it off the main thread.
 */
object JpegTurner {
    private const val MIRROR = -1f
    private const val SAME = 1f

    fun upright(jpeg: ByteArray): ByteArray {
        val orientation = ExifInterface(ByteArrayInputStream(jpeg))
            .getAttributeInt(ExifInterface.TAG_ORIENTATION, ExifInterface.ORIENTATION_NORMAL)
        val turn = ExifTurnLogic.turn(orientation) ?: return jpeg
        val source = BitmapFactory.decodeByteArray(jpeg, 0, jpeg.size) ?: return jpeg
        val matrix = Matrix().apply {
            setRotate(turn.degrees.toFloat())
            if (turn.mirror) postScale(MIRROR, SAME)
        }
        val turned = Bitmap.createBitmap(source, 0, 0, source.width, source.height, matrix, true)
        if (turned !== source) source.recycle()
        return ByteArrayOutputStream().use { out ->
            turned.compress(Bitmap.CompressFormat.JPEG, Constants.Snapshot.JPEG_QUALITY, out)
            turned.recycle()
            out.toByteArray()
        }
    }
}
