package dev.jayson.debugdevices.camera

import androidx.exifinterface.media.ExifInterface
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertSame
import org.junit.Assert.assertThrows
import org.junit.Test

/**
 * N9 and C13: an EXIF-turned still is turned in its pixels, or the snapshot fails; it is never sent with an EXIF turn.
 * The EXIF reader and the bitmap turner need Android, so the tests give fakes (the S22 check covers the real ones).
 */
class JpegTurnerTest {
    private val still = byteArrayOf(0xFF.toByte(), 0xD8.toByte(), 0xFF.toByte(), 0xD9.toByte())
    private val turnedBytes = byteArrayOf(1, 2, 3)

    private fun orientation(value: Int): (ByteArray) -> Int = { value }

    @Test
    fun `an EXIF-turned still is turned by the pixel turner`() {
        var asked: ExifTurn? = null
        val turner = JpegTurner(orientation(ExifInterface.ORIENTATION_ROTATE_90)) { _, turn ->
            asked = turn
            turnedBytes
        }
        assertArrayEquals(turnedBytes, turner.upright(still))
        assertEquals(ExifTurn(90, mirror = false), asked)
    }

    @Test
    fun `no EXIF turn gives the same bytes and no decode`() {
        for (value in listOf(ExifInterface.ORIENTATION_NORMAL, ExifInterface.ORIENTATION_UNDEFINED)) {
            val turner = JpegTurner(orientation(value)) { _, _ -> error("must not decode") }
            assertSame(still, turner.upright(still))
        }
    }

    @Test
    fun `a failed decode is 500 capture_failed, never the EXIF-turned still`() {
        val turner = JpegTurner(orientation(ExifInterface.ORIENTATION_ROTATE_270)) { _, _ -> null }
        val error = assertThrows(ApiException::class.java) { turner.upright(still) }
        assertEquals(ErrorCode.CAPTURE_FAILED, error.code)
        assertEquals("could not turn the still", error.message)
    }

    @Test
    fun `out of memory while turning is 500 capture_failed`() {
        val turner = JpegTurner(orientation(ExifInterface.ORIENTATION_ROTATE_180)) { _, _ ->
            throw OutOfMemoryError("two bitmaps")
        }
        val error = assertThrows(ApiException::class.java) { turner.upright(still) }
        assertEquals(ErrorCode.CAPTURE_FAILED, error.code)
    }
}
