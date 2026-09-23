package dev.jayson.debugdevices.camera

import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.yield
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test

class ControlGateTest {
    @Test
    fun `control is 503 before the start state`() = runTest {
        val gate = ControlGate()
        val error = runCatching { gate.control { 1 } }.exceptionOrNull() as ApiException
        assertEquals(ErrorCode.CAMERA_NOT_READY, error.code)
        assertThrows(ApiException::class.java) { gate.checkReady() }
    }

    @Test
    fun `control is 503 at once while the start state runs`() = runTest {
        val gate = ControlGate()
        val startRunning = CompletableDeferred<Unit>()
        val finishStart = CompletableDeferred<Unit>()
        val start = launch {
            gate.start {
                startRunning.complete(Unit)
                finishStart.await()
            }
        }
        startRunning.await()
        // Bug 1: a zoom during the start state must not reach CameraX. It gets 503 and does not wait.
        val error = runCatching { gate.control { 1 } }.exceptionOrNull() as ApiException
        assertEquals(ErrorCode.CAMERA_NOT_READY, error.code)
        assertFalse(gate.isReady)
        finishStart.complete(Unit)
        start.join()
        assertTrue(gate.isReady)
        assertEquals(1, gate.control { 1 })
    }

    @Test
    fun `a failed start state still opens the gate`() = runTest {
        val gate = ControlGate()
        val error = runCatching { gate.start { throw ApiException(ErrorCode.CAMERA_NOT_READY, "closed") } }
        assertTrue(error.isFailure)
        assertTrue(gate.isReady)
        assertEquals(2, gate.control { 2 })
    }

    @Test
    fun `changes run one at a time and none is cancelled`() = runTest {
        val gate = ControlGate()
        gate.start {}
        var running = 0
        var maxRunning = 0
        val results = (1..CONCURRENT_REQUESTS).map { index ->
            async {
                gate.control {
                    running++
                    maxRunning = maxOf(maxRunning, running)
                    yield()
                    delay(STEP_DELAY_MILLIS)
                    running--
                    index
                }
            }
        }.awaitAll()
        assertEquals(1, maxRunning)
        assertEquals((1..CONCURRENT_REQUESTS).toList(), results)
    }

    private companion object {
        const val CONCURRENT_REQUESTS = 20
        const val STEP_DELAY_MILLIS = 10L
    }
}
