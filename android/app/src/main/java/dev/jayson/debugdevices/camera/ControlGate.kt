package dev.jayson.debugdevices.camera

import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withTimeoutOrNull

/**
 * Runs camera changes (zoom, torch, start state, new binds) and snapshots one at a time, and refuses them until the
 * start state is set.
 *
 * CameraX cancels a pending zoom or torch call when a new one arrives. One lock for all changes means a request
 * never cancels another one. Pure Kotlin, so JVM unit tests cover it.
 */
class ControlGate(private val lock: Mutex = Mutex()) {

    @Volatile
    var isReady = false
        private set

    /** Throws 503 `camera_not_ready` until [start] is done. */
    fun checkReady() {
        if (!isReady) throw ApiException(ErrorCode.CAMERA_NOT_READY, Constants.Messages.START_STATE_PENDING)
    }

    /** Runs one camera change under the lock. Refuses at once (without a wait) while the start state runs. */
    suspend fun <T> control(block: suspend () -> T): T {
        checkReady()
        return lock.withLock {
            checkReady()
            block()
        }
    }

    /**
     * Runs a snapshot under the same lock as the camera changes (N45): a change waits for a running snapshot, and a
     * snapshot waits for a running change (for example a new bind) at most [waitMillis], then gets 503
     * "camera change still running". The wait never cancels the change. Refuses at once while the start state runs.
     */
    suspend fun <T> snapshot(waitMillis: Long, block: suspend () -> T): T {
        checkReady()
        // N46: the timeout can fire after lock() returned, right before the wait block ends. Then the block result is
        // lost but the lock is taken: the flag keeps that lock owned by this snapshot, so the finally unlocks it.
        var locked = false
        withTimeoutOrNull(waitMillis) {
            lock.lock()
            locked = true
        }
        if (!locked) throw ApiException(ErrorCode.CAMERA_NOT_READY, Constants.Messages.CAMERA_CHANGE_RUNNING)
        try {
            checkReady()
            return block()
        } finally {
            lock.unlock()
        }
    }

    /**
     * Runs the start state under the lock, then opens the gate. The gate opens also when [block] fails, so a
     * failed start state does not block the API. The caller logs the failure.
     */
    suspend fun start(block: suspend () -> Unit) {
        lock.withLock {
            try {
                block()
            } finally {
                isReady = true
            }
        }
    }
}
