package dev.jayson.debugdevices.camera

import kotlinx.coroutines.delay
import kotlinx.coroutines.withTimeoutOrNull

/**
 * Checks [condition] every [pollMillis] until it is true or [timeoutMillis] ends; true when it became true. The
 * delay lets the main thread run its layout pass between the checks (N11: the rotation response waits for it).
 */
suspend fun awaitCondition(timeoutMillis: Long, pollMillis: Long, condition: () -> Boolean): Boolean =
    withTimeoutOrNull(timeoutMillis) {
        while (!condition()) delay(pollMillis)
        true
    } ?: false
