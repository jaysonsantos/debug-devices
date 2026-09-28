package dev.jayson.debugdevices.camera

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Matrix
import androidx.exifinterface.media.ExifInterface
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.io.IOException

/** Decodes a JPEG, turns (and mirrors) its pixels, and encodes it again; null when that fails. */
fun interface PixelTurner {
    fun turn(jpeg: ByteArray, turn: ExifTurn): ByteArray?
}

/**
 * Makes a still JPEG upright in its pixels (C13 of the contract). A JPEG with EXIF `Orientation` 1 or none comes back
 * unchanged (the same bytes). Else [pixels] turns it, and the result has no EXIF block (`Orientation` is absent). When
 * the turn fails, it throws 500 `capture_failed` "could not turn the still": the app never returns a still with an
 * EXIF turn. Slow and memory-heavy on the phone (two full bitmaps): call it off the main thread.
 */
class JpegTurner(
    private val readOrientation: (ByteArray) -> Int = ::exifOrientation,
    private val pixels: PixelTurner = BitmapPixelTurner
) {
    fun upright(jpeg: ByteArray): ByteArray {
        val turn = ExifTurnLogic.turn(readOrientation(jpeg)) ?: return jpeg
        val turned = try {
            pixels.turn(jpeg, turn)
        } catch (cause: OutOfMemoryError) {
            throw failed(cause)
        } catch (cause: IllegalArgumentException) {
            throw failed(cause)
        }
        return turned ?: throw failed(null)
    }

    private fun failed(cause: Throwable?) =
        ApiException(ErrorCode.CAPTURE_FAILED, Constants.Messages.STILL_TURN_FAILED, cause)
}

/** The EXIF orientation of a JPEG, or normal when it has no readable EXIF block (`ExifInterface` needs Android). */
fun exifOrientation(jpeg: ByteArray): Int = try {
    ExifInterface(ByteArrayInputStream(jpeg))
        .getAttributeInt(ExifInterface.TAG_ORIENTATION, ExifInterface.ORIENTATION_NORMAL)
} catch (_: IOException) {
    ExifInterface.ORIENTATION_NORMAL
}

/** The phone's [PixelTurner]: Android bitmaps. */
object BitmapPixelTurner : PixelTurner {
    private const val MIRROR = -1f
    private const val SAME = 1f

    override fun turn(jpeg: ByteArray, turn: ExifTurn): ByteArray? {
        val source = BitmapFactory.decodeByteArray(jpeg, 0, jpeg.size) ?: return null
        val matrix = Matrix().apply {
            setRotate(turn.degrees.toFloat())
            if (turn.mirror) postScale(MIRROR, SAME)
        }
        val turned = Bitmap.createBitmap(source, 0, 0, source.width, source.height, matrix, true)
        if (turned !== source) source.recycle()
        return ByteArrayOutputStream().use { out ->
            val encoded = turned.compress(Bitmap.CompressFormat.JPEG, Constants.Snapshot.JPEG_QUALITY, out)
            turned.recycle()
            if (encoded) out.toByteArray() else null
        }
    }
}
