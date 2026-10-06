package dev.jayson.debugdevices.camera

import android.net.Uri
import android.service.notification.ConditionProviderService

/**
 * Turns the rule off after the app process dies while the app is visible (a crash, a kill, `am force-stop`, a new
 * install): a dead process cannot turn its rule off, and Android keeps the rule on.
 *
 * Android binds a condition provider service while the app has the Do Not Disturb access, and it starts the process
 * again after the process dies. Each new connection turns off a rule that has no visible instance
 * ([QuietMode.reset]). The cost: the process stays in memory while the app has the access. It starts no camera and
 * no server, because those follow the activity.
 *
 * The rule does not use this service for its state (`setAutomaticZenRuleState` does that), so the subscriptions are
 * empty. Android deprecated the class as a source of rule state, not the binding.
 */
@Suppress("DEPRECATION", "OVERRIDE_DEPRECATION")
class QuietModeGuard : ConditionProviderService() {
    override fun onConnected() = ProcessQuietMode.of(this).reset()

    override fun onSubscribe(conditionId: Uri) = Unit

    override fun onUnsubscribe(conditionId: Uri) = Unit
}
