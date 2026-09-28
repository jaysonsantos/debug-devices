package dev.jayson.debugdevices.camera

import androidx.exifinterface.media.ExifInterface
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/** C13: the still is turned in its pixels; the EXIF tag only says how. */
class ExifTurnLogicTest {
    @Test
    fun `upright tags need no turn`() {
        assertNull(ExifTurnLogic.turn(ExifInterface.ORIENTATION_NORMAL))
        assertNull(ExifTurnLogic.turn(ExifInterface.ORIENTATION_UNDEFINED))
        assertNull(ExifTurnLogic.turn(42))
    }

    @Test
    fun `every EXIF orientation maps to a turn and a mirror`() {
        assertEquals(ExifTurn(0, mirror = true), ExifTurnLogic.turn(ExifInterface.ORIENTATION_FLIP_HORIZONTAL))
        assertEquals(ExifTurn(180, mirror = false), ExifTurnLogic.turn(ExifInterface.ORIENTATION_ROTATE_180))
        assertEquals(ExifTurn(180, mirror = true), ExifTurnLogic.turn(ExifInterface.ORIENTATION_FLIP_VERTICAL))
        assertEquals(ExifTurn(90, mirror = true), ExifTurnLogic.turn(ExifInterface.ORIENTATION_TRANSPOSE))
        assertEquals(ExifTurn(90, mirror = false), ExifTurnLogic.turn(ExifInterface.ORIENTATION_ROTATE_90))
        assertEquals(ExifTurn(270, mirror = true), ExifTurnLogic.turn(ExifInterface.ORIENTATION_TRANSVERSE))
        assertEquals(ExifTurn(270, mirror = false), ExifTurnLogic.turn(ExifInterface.ORIENTATION_ROTATE_270))
    }

    @Test
    fun `a quarter turn swaps width and height`() {
        // The S22 writes a landscape 4080 x 3060 JPEG with Orientation 6 for a portrait still.
        assertEquals(
            3060 to 4080,
            ExifTurnLogic.turnedSize(4080, 3060, ExifTurnLogic.turn(ExifInterface.ORIENTATION_ROTATE_90))
        )
        assertEquals(
            4080 to 3060,
            ExifTurnLogic.turnedSize(4080, 3060, ExifTurnLogic.turn(ExifInterface.ORIENTATION_ROTATE_180))
        )
        assertEquals(4080 to 3060, ExifTurnLogic.turnedSize(4080, 3060, null))
    }
}
