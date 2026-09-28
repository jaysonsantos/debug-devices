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

/**
 * Runs [change], waits for the layout pass that it starts, then returns [read] (N11). It waits only when the view
 * was [measured] before the change: else no layout pass of the new orientation comes from it. When the wait ends
 * without that pass, [onTimeout] runs (N23: a log line) and [read] still runs (with overlay_region null). [read]
 * runs after the wait, so it sees the app state after the wait (N22: a pause during the wait gives 503).
 */
suspend fun <T> changeThenAwaitLayout(
    measured: () -> Boolean,
    change: () -> Unit,
    onTimeout: () -> Unit,
    read: () -> T
): T {
    val measuredBefore = measured()
    change()
    val laidOut = !measuredBefore ||
        awaitCondition(
            Constants.Overlay.ROTATION_LAYOUT_WAIT_MILLIS,
            Constants.Overlay.ROTATION_LAYOUT_POLL_MILLIS,
            measured
        )
    if (!laidOut) onTimeout()
    return read()
}
