package dev.jayson.debugdevices.camera

import kotlinx.coroutines.test.currentTime
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** N11: the rotation response waits for the layout pass of the new orientation, at most a short time. */
class WaitsTest {
    private val timeout = Constants.Overlay.LAYOUT_WAIT_MILLIS
    private val poll = Constants.Overlay.LAYOUT_POLL_MILLIS

    @Test
    fun `returns at once when the condition already holds`() = runTest {
        assertTrue(awaitCondition(timeout, poll) { true })
        assertEquals(0L, currentTime)
    }

    @Test
    fun `waits until the layout pass has run`() = runTest {
        var checks = 0
        assertTrue(awaitCondition(timeout, poll) { ++checks > 2 })
        assertEquals(2 * poll, currentTime)
    }

    @Test
    fun `gives up after the timeout`() = runTest {
        assertFalse(awaitCondition(timeout, poll) { false })
        assertTrue(currentTime >= timeout)
    }
}
