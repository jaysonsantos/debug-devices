package dev.jayson.debugdevices.camera

import android.app.AutomaticZenRule
import android.app.NotificationManager
import android.content.ComponentName
import android.content.Context
import android.net.Uri
import android.os.Build
import android.service.notification.Condition
import android.service.notification.ZenPolicy
import android.util.Log
import androidx.annotation.RequiresApi

/**
 * The app's Do Not Disturb rule in Android, found by its condition id. An app can turn its own rule on and off
 * from Android 10 on (`setAutomaticZenRuleState`).
 *
 * While the rule is on, calls, messages, and all other notifications make no sound, no vibration, and no picture on
 * the screen. Alarms and media still play. Android shows the notifications again when the rule goes off.
 */
@RequiresApi(Build.VERSION_CODES.Q)
class ZenRules(private val context: Context) : ZenRulePort {
    private val manager = context.getSystemService(NotificationManager::class.java)
    private val conditionId: Uri = Uri.Builder()
        .scheme(Condition.SCHEME)
        .authority(context.packageName)
        .appendPath(Constants.QuietMode.CONDITION_PATH)
        .build()

    override val accessGranted: Boolean
        get() = manager.isNotificationPolicyAccessGranted

    override fun findRule(): String? =
        manager.automaticZenRules.entries.firstOrNull { it.value.conditionId == conditionId }?.key

    override fun addRule(): String? {
        val policy = ZenPolicy.Builder()
            .disallowAllSounds()
            .allowAlarms(true)
            .allowMedia(true)
            .hideAllVisualEffects()
            .build()
        // A ZenPolicy applies only with the PRIORITY filter. The activity is the rule's settings page: a rule needs
        // one when it has no condition provider service.
        val rule = AutomaticZenRule(
            context.getString(R.string.quiet_rule_name),
            null,
            ComponentName(context, MainActivity::class.java),
            conditionId,
            policy,
            NotificationManager.INTERRUPTION_FILTER_PRIORITY,
            true
        )
        return manager.addAutomaticZenRule(rule)
    }

    override fun setActive(ruleId: String, active: Boolean) {
        val state = if (active) Condition.STATE_TRUE else Condition.STATE_FALSE
        val summary = context.getString(R.string.quiet_condition_summary)
        manager.setAutomaticZenRuleState(ruleId, Condition(conditionId, summary, state))
    }
}

/** The quiet mode of the app process: activity instances come and go, the rule stays one. Main thread only. */
object ProcessQuietMode {
    private var instance: QuietMode? = null

    fun of(context: Context): QuietMode = instance ?: QuietMode(
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) ZenRules(context.applicationContext) else null,
        ::logQuietEvent
    ).also { instance = it }
}

private fun logQuietEvent(message: String, cause: Throwable?) {
    if (cause == null) Log.i(Constants.Log.TAG, message) else Log.w(Constants.Log.TAG, message, cause)
}
