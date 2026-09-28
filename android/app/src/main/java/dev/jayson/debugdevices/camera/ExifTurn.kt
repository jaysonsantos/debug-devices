package dev.jayson.debugdevices.camera

import androidx.exifinterface.media.ExifInterface

/** How to make a JPEG upright: turn clockwise by [degrees], then mirror left-right when [mirror] is true. */
data class ExifTurn(val degrees: Int, val mirror: Boolean)

/**
 * The contract: the app turns the pixels of the `/v1/snapshot` JPEG, and the EXIF `Orientation` tag is 1 or absent.
 * Some phones (the S22) write the turn as an EXIF tag instead. This maps that tag to the pixel turn. Pure, so JVM unit
 * tests cover it.
 */
object ExifTurnLogic {
    private const val QUARTER = 90
    private const val HALF = 180
    private const val THREE_QUARTERS = 270

    /** Null when the pixels are already upright (Orientation 1, 0 = undefined, or an unknown value). */
    fun turn(orientation: Int): ExifTurn? = when (orientation) {
        ExifInterface.ORIENTATION_FLIP_HORIZONTAL -> ExifTurn(0, mirror = true)
        ExifInterface.ORIENTATION_ROTATE_180 -> ExifTurn(HALF, mirror = false)
        ExifInterface.ORIENTATION_FLIP_VERTICAL -> ExifTurn(HALF, mirror = true)
        ExifInterface.ORIENTATION_TRANSPOSE -> ExifTurn(QUARTER, mirror = true)
        ExifInterface.ORIENTATION_ROTATE_90 -> ExifTurn(QUARTER, mirror = false)
        ExifInterface.ORIENTATION_TRANSVERSE -> ExifTurn(THREE_QUARTERS, mirror = true)
        ExifInterface.ORIENTATION_ROTATE_270 -> ExifTurn(THREE_QUARTERS, mirror = false)
        else -> null
    }

    /** Width and height after the turn. */
    fun turnedSize(width: Int, height: Int, turn: ExifTurn?): Pair<Int, Int> =
        if (turn != null && turn.degrees % HALF != 0) height to width else width to height
}
