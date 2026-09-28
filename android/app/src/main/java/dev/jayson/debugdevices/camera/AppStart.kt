package dev.jayson.debugdevices.camera

import java.security.SecureRandom
import java.util.Random
import java.util.UUID

/** RFC 9562 UUID version 7: a 48-bit Unix time in milliseconds, then random bits. Sorts by creation time. */
object UuidV7 {
    private const val TIMESTAMP_BITS = 48
    private const val TIMESTAMP_MASK = (1L shl TIMESTAMP_BITS) - 1
    private const val VERSION_AND_RANDOM_BITS = 16
    private const val VERSION_7 = 0x7000L
    private const val RANDOM_A_MASK = 0x0FFFL
    private const val RANDOM_B_MASK = 0x3FFF_FFFF_FFFF_FFFFL
    private const val VARIANT_RFC = Long.MIN_VALUE // the bits 10 at the top of the low half

    fun create(epochMillis: Long, random: Random): UUID {
        val high = ((epochMillis and TIMESTAMP_MASK) shl VERSION_AND_RANDOM_BITS) or VERSION_7 or
            (random.nextLong() and RANDOM_A_MASK)
        val low = (random.nextLong() and RANDOM_B_MASK) or VARIANT_RFC
        return UUID(high, low)
    }
}

/** `CameraStatus.app_start_id`: made once when the app starts (one per controller), then stable. */
class AppStart(clock: () -> Long = System::currentTimeMillis, random: Random = SecureRandom()) {
    val id: String = UuidV7.create(clock(), random).toString()
}
