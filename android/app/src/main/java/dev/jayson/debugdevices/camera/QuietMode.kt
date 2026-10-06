package dev.jayson.debugdevices.camera

/** What [QuietMode] needs from Android's Do Not Disturb. A call can throw a [RuntimeException]. */
interface ZenRulePort {
    /** True after the user gave the app Do Not Disturb access. */
    val accessGranted: Boolean

    /** The id of the app's rule, or null when there is none (first use, or the user deleted it). */
    fun findRule(): String?

    /** Makes the app's rule and returns its id, or null when Android refuses it. */
    fun addRule(): String?

    /** Turns the rule on or off. Only a rule that is on silences notifications. */
    fun setActive(ruleId: String, active: Boolean)
}

/** The result of [QuietMode.enter]. Only [ON] silences notifications. */
enum class QuietState(val message: String) {
    ON(Constants.Messages.QUIET_ON),
    NO_ACCESS(Constants.Messages.QUIET_NO_ACCESS),
    UNSUPPORTED(Constants.Messages.QUIET_UNSUPPORTED),
    FAILED(Constants.Messages.QUIET_FAILED)
}

/**
 * Silences notifications while the app is visible: a heads-up notification covers the preview in the screen stream,
 * and a sound or a vibration moves the phone. It turns the app's own Do Not Disturb rule on and off, so the user's
 * Do Not Disturb settings do not change.
 *
 * One for the process: a new activity instance can be visible before the old one stops (the order of N21), so the
 * rule goes off only when no instance is visible. A null [port] means that this Android version has no such rule.
 * No error escapes: [log] gets it. Not thread-safe: call it on the main thread.
 */
class QuietMode(private val port: ZenRulePort?, private val log: (String, Throwable?) -> Unit) {
    /** The activity instances that are visible now. */
    private val visible = mutableSetOf<Any>()

    /** The rule that is on, or null while it is off. */
    private var activeRuleId: String? = null

    /** The last result that went to [log], so a repeated result logs nothing. */
    private var logged: QuietState? = null

    /** [owner] is visible. A second call tries again, for example after the user gave the access. */
    fun enter(owner: Any): QuietState {
        visible += owner
        if (activeRuleId != null) return QuietState.ON
        val (state, cause) = try {
            turnOn() to null
        } catch (cause: RuntimeException) {
            QuietState.FAILED to cause
        }
        if (state != logged) {
            logged = state
            log(state.message, cause)
        }
        return state
    }

    /** [owner] is not visible. The rule goes off after the last visible owner. */
    fun leave(owner: Any) {
        visible -= owner
        if (visible.isNotEmpty()) return
        val ruleId = activeRuleId ?: return
        activeRuleId = null
        logged = null
        try {
            port?.setActive(ruleId, false)
            log(Constants.Messages.QUIET_OFF, null)
        } catch (cause: RuntimeException) {
            log(Constants.Messages.QUIET_OFF_FAILED, cause)
        }
    }

    /**
     * Turns off a rule that a dead process left on. A process that Android stops while the app is visible (a crash,
     * a kill, a force-stop, a new install) cannot turn its rule off. Call it when a process starts: it does nothing
     * while an instance is visible.
     */
    fun reset() {
        if (visible.isNotEmpty()) return
        try {
            val port = port ?: return
            if (!port.accessGranted) return
            val ruleId = port.findRule() ?: return
            port.setActive(ruleId, false)
            log(Constants.Messages.QUIET_RESET, null)
        } catch (cause: RuntimeException) {
            log(Constants.Messages.QUIET_OFF_FAILED, cause)
        }
    }

    private fun turnOn(): QuietState {
        val port = port ?: return QuietState.UNSUPPORTED
        if (!port.accessGranted) return QuietState.NO_ACCESS
        val ruleId = port.findRule() ?: port.addRule() ?: return QuietState.FAILED
        port.setActive(ruleId, true)
        activeRuleId = ruleId
        return QuietState.ON
    }
}
