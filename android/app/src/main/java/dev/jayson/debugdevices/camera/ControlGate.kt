package dev.jayson.debugdevices.camera

import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock

/**
 * Runs camera changes (zoom, torch, start state) one at a time, and refuses them until the start state is set.
 *
 * CameraX cancels a pending zoom or torch call when a new one arrives. One lock for all changes means a request
 * never cancels another one. Pure Kotlin, so JVM unit tests cover it.
 */
class ControlGate {
    private val lock = Mutex()

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
