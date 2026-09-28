package dev.jayson.debugdevices.camera

import java.util.Random
import java.util.UUID
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class AppStartTest {
    private val millis = 0x0192_F3A4_5B6CL

    @Test
    fun `uuid v7 layout`() {
        val uuid = UuidV7.create(millis, Random(1))
        assertEquals(7, uuid.version())
        assertEquals(2, uuid.variant())
        assertEquals(millis, uuid.mostSignificantBits ushr 16)
        assertTrue(uuid.toString().startsWith("0192f3a4-5b6c-7"))
    }

    @Test
    fun `later ids sort after earlier ids`() {
        val random = Random(2)
        val first = UuidV7.create(millis, random).toString()
        val second = UuidV7.create(millis + 1, random).toString()
        assertTrue(first < second)
    }

    @Test
    fun `stable across calls, new after a restart`() {
        val start = AppStart(clock = { millis })
        assertEquals(start.id, start.id)
        assertEquals(7, UUID.fromString(start.id).version())
        val restart = AppStart(clock = { millis + 1 })
        assertNotEquals(start.id, restart.id)
        // Also a different id when the clock is the same (random bits).
        assertNotEquals(AppStart(clock = { millis }).id, AppStart(clock = { millis }).id)
    }
}
