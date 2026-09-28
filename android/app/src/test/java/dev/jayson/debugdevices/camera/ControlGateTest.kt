package dev.jayson.debugdevices.camera

import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.currentTime
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.yield
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test

@OptIn(ExperimentalCoroutinesApi::class)
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

    private fun snapshotError(error: Throwable?) = (error as ApiException).let { it.code to it.message }

    @Test
    fun `a snapshot waits for a running change, then runs (N44)`() = runTest {
        val gate = ControlGate().apply { start {} }
        var changeDone = false
        launch {
            gate.control {
                delay(SLOW_CHANGE_MILLIS)
                changeDone = true
            }
        }
        runCurrent()
        val snapshot =
            async { gate.snapshot(Constants.Snapshot.SNAPSHOT_READY_WAIT_MILLIS) { changeDone to currentTime } }
        assertEquals(true to SLOW_CHANGE_MILLIS, snapshot.await())
    }

    @Test
    fun `a change longer than the wait gives the snapshot 503, and the change still ends (N44)`() = runTest {
        val gate = ControlGate().apply { start {} }
        val wait = Constants.Snapshot.SNAPSHOT_READY_WAIT_MILLIS
        var changeDone = false
        val change = launch {
            gate.control {
                delay(wait + SLOW_CHANGE_MILLIS)
                changeDone = true
            }
        }
        runCurrent()
        val error = runCatching { gate.snapshot(wait) { 1 } }.exceptionOrNull()
        assertEquals(ErrorCode.CAMERA_NOT_READY to Constants.Messages.CAMERA_CHANGE_RUNNING, snapshotError(error))
        assertEquals(wait, currentTime)
        change.join()
        assertTrue(changeDone)
        // The lock is free again for the next snapshot.
        assertEquals(2, gate.snapshot(wait) { 2 })
    }

    @Test
    fun `a change waits for a running snapshot (N44)`() = runTest {
        val gate = ControlGate().apply { start {} }
        var snapshotDone = false
        launch {
            gate.snapshot(Constants.Snapshot.SNAPSHOT_READY_WAIT_MILLIS) {
                delay(SLOW_CHANGE_MILLIS)
                snapshotDone = true
            }
        }
        runCurrent()
        assertEquals(true to SLOW_CHANGE_MILLIS, gate.control { snapshotDone to currentTime })
    }

    @Test
    fun `a snapshot is 503 at once while the start state runs`() = runTest {
        val gate = ControlGate()
        val finishStart = CompletableDeferred<Unit>()
        val start = launch { gate.start { finishStart.await() } }
        runCurrent()
        val error = runCatching { gate.snapshot(Constants.Snapshot.SNAPSHOT_READY_WAIT_MILLIS) { 1 } }.exceptionOrNull()
        assertEquals(ErrorCode.CAMERA_NOT_READY to Constants.Messages.START_STATE_PENDING, snapshotError(error))
        assertEquals(0L, currentTime)
        finishStart.complete(Unit)
        start.join()
    }

    private companion object {
        const val CONCURRENT_REQUESTS = 20
        const val STEP_DELAY_MILLIS = 10L
        const val SLOW_CHANGE_MILLIS = 1_000L
    }
}
