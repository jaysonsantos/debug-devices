package dev.jayson.debugdevices.camera

import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.currentTime
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** N11: the rotation response waits for the layout pass of the new orientation, at most a short time. */
@OptIn(ExperimentalCoroutinesApi::class)
class WaitsTest {
    private val timeout = Constants.Overlay.ROTATION_LAYOUT_WAIT_MILLIS
    private val poll = Constants.Overlay.ROTATION_LAYOUT_POLL_MILLIS

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

    /** The rotation wiring of the controller: the view, the rotation lock, the log, and the status read. */
    private class Rotation(private val measuredBefore: Boolean, private val layoutAfterChecks: Int?) {
        var changed = false
        var timeouts = 0
        val reads = mutableListOf<Long>()
        private var checks = 0

        /** A turn between portrait and sideways: not measured from the change until the layout pass (null: never). */
        fun measured(): Boolean = if (!changed) measuredBefore else layoutAfterChecks?.let { checks++ >= it } ?: false

        suspend fun run(clock: () -> Long): Long = changeThenAwaitLayout(
            measured = ::measured,
            change = { changed = true },
            onTimeout = { timeouts++ },
            read = { clock().also { reads += it } }
        )
    }

    @Test
    fun `the rotation response is read after the layout pass (N11)`() = runTest {
        val rotation = Rotation(measuredBefore = true, layoutAfterChecks = 2)
        assertEquals(2 * poll, rotation.run { currentTime })
        assertEquals(0, rotation.timeouts)
    }

    @Test
    fun `a wait without a layout pass logs once and still answers (N23)`() = runTest {
        val rotation = Rotation(measuredBefore = true, layoutAfterChecks = null)
        val readAt = rotation.run { currentTime }
        assertTrue(readAt >= timeout)
        assertEquals(1, rotation.timeouts)
        assertEquals(listOf(readAt), rotation.reads)
    }

    @Test
    fun `a view that was not measured before gets no wait`() = runTest {
        val rotation = Rotation(measuredBefore = false, layoutAfterChecks = null)
        assertEquals(0L, rotation.run { currentTime })
        assertEquals(0, rotation.timeouts)
    }

    @Test
    fun `the status read after the wait sees a pause during the wait (N22)`() = runTest {
        var checks = 0
        var paused = false
        val error = runCatching {
            changeThenAwaitLayout(
                // Measured before the change; the app pauses at the first check; the layout pass ends at the second.
                measured = {
                    when (checks++) {
                        0 -> true
                        1 -> false.also { paused = true }
                        else -> true
                    }
                },
                change = {},
                onTimeout = {},
                read = {
                    if (paused) throw ApiException(ErrorCode.CAMERA_NOT_READY, Constants.Messages.CAMERA_NOT_ACTIVE)
                }
            )
        }.exceptionOrNull()
        assertEquals(ErrorCode.CAMERA_NOT_READY, (error as ApiException).code)
    }
}
