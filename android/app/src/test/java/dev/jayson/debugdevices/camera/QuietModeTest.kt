package dev.jayson.debugdevices.camera

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Test

class QuietModeTest {
    private class FakeZenRules(override var accessGranted: Boolean = true) : ZenRulePort {
        var ruleId: String? = null
        var addedRules = 0
        var refuseNewRule = false
        var failure: RuntimeException? = null

        /** Every [setActive] call, in order. */
        val switches = mutableListOf<Boolean>()

        override fun findRule(): String? = ruleId

        override fun addRule(): String? {
            if (refuseNewRule) return null
            addedRules += 1
            return RULE_ID.also { ruleId = it }
        }

        override fun setActive(ruleId: String, active: Boolean) {
            failure?.let { throw it }
            assertEquals(this.ruleId, ruleId)
            switches += active
        }
    }

    private val rules = FakeZenRules()
    private val logs = mutableListOf<Pair<String, Throwable?>>()
    private val quiet = QuietMode(rules) { message, cause -> logs += message to cause }
    private val first = Any()
    private val second = Any()

    @Test
    fun `the first visible instance makes the rule and turns it on`() {
        assertEquals(QuietState.ON, quiet.enter(first))
        assertEquals(1, rules.addedRules)
        assertEquals(listOf(true), rules.switches)
        assertEquals(listOf<Pair<String, Throwable?>>(Constants.Messages.QUIET_ON to null), logs)
    }

    @Test
    fun `the rule goes off when the instance is not visible`() {
        quiet.enter(first)
        quiet.leave(first)
        assertEquals(listOf(true, false), rules.switches)
        assertEquals(listOf(Constants.Messages.QUIET_ON, Constants.Messages.QUIET_OFF), logs.map { it.first })
    }

    @Test
    fun `a rule that exists is used again`() {
        rules.ruleId = RULE_ID
        assertEquals(QuietState.ON, quiet.enter(first))
        quiet.leave(first)
        assertEquals(QuietState.ON, quiet.enter(first))
        assertEquals(0, rules.addedRules)
        assertEquals(listOf(true, false, true), rules.switches)
    }

    @Test
    fun `the rule stays on while a new instance is visible before the old one stops`() {
        quiet.enter(first)
        assertEquals(QuietState.ON, quiet.enter(second))
        quiet.leave(first)
        assertEquals(listOf(true), rules.switches)
        quiet.leave(second)
        assertEquals(listOf(true, false), rules.switches)
    }

    @Test
    fun `a second leave does nothing`() {
        quiet.enter(first)
        quiet.leave(first)
        quiet.leave(first)
        quiet.leave(second)
        assertEquals(listOf(true, false), rules.switches)
    }

    @Test
    fun `without access nothing changes, and one log line tells why`() {
        rules.accessGranted = false
        assertEquals(QuietState.NO_ACCESS, quiet.enter(first))
        assertEquals(QuietState.NO_ACCESS, quiet.enter(first))
        quiet.leave(first)
        assertEquals(0, rules.addedRules)
        assertEquals(emptyList<Boolean>(), rules.switches)
        assertEquals(listOf(Constants.Messages.QUIET_NO_ACCESS), logs.map { it.first })
    }

    @Test
    fun `access that comes while the app is visible turns the rule on`() {
        rules.accessGranted = false
        quiet.enter(first)
        rules.accessGranted = true
        assertEquals(QuietState.ON, quiet.enter(first))
        quiet.leave(first)
        assertEquals(listOf(true, false), rules.switches)
    }

    @Test
    fun `an Android version without the rule changes nothing`() {
        val unsupported = QuietMode(null) { message, cause -> logs += message to cause }
        assertEquals(QuietState.UNSUPPORTED, unsupported.enter(first))
        unsupported.leave(first)
        assertEquals(listOf<Pair<String, Throwable?>>(Constants.Messages.QUIET_UNSUPPORTED to null), logs)
    }

    @Test
    fun `a rule that Android refuses is a failure`() {
        rules.refuseNewRule = true
        assertEquals(QuietState.FAILED, quiet.enter(first))
        quiet.leave(first)
        assertEquals(emptyList<Boolean>(), rules.switches)
        assertEquals(listOf(Constants.Messages.QUIET_FAILED), logs.map { it.first })
    }

    @Test
    fun `an error from Android does not escape`() {
        val failure = SecurityException("no access")
        rules.failure = failure
        assertEquals(QuietState.FAILED, quiet.enter(first))
        assertEquals(Constants.Messages.QUIET_FAILED, logs.single().first)
        assertSame(failure, logs.single().second)
        // The rule is not on, so the stop has nothing to turn off.
        quiet.leave(first)
        assertEquals(1, logs.size)
    }

    @Test
    fun `an error at the stop does not escape, and the next start works`() {
        quiet.enter(first)
        val failure = IllegalStateException("gone")
        rules.failure = failure
        quiet.leave(first)
        assertEquals(Constants.Messages.QUIET_OFF_FAILED, logs.last().first)
        assertSame(failure, logs.last().second)
        rules.failure = null
        assertEquals(QuietState.ON, quiet.enter(first))
        assertEquals(listOf(true, true), rules.switches)
        assertNull(logs.last().second)
    }

    @Test
    fun `a new process turns off the rule that a dead process left on`() {
        rules.ruleId = RULE_ID
        quiet.reset()
        assertEquals(listOf(false), rules.switches)
        assertEquals(listOf<Pair<String, Throwable?>>(Constants.Messages.QUIET_RESET to null), logs)
    }

    @Test
    fun `the reset keeps the rule on while an instance is visible`() {
        quiet.enter(first)
        quiet.reset()
        assertEquals(listOf(true), rules.switches)
    }

    @Test
    fun `the reset does nothing without a rule, without access, or without rule support`() {
        quiet.reset()
        rules.ruleId = RULE_ID
        rules.accessGranted = false
        quiet.reset()
        QuietMode(null) { message, cause -> logs += message to cause }.reset()
        assertEquals(0, rules.addedRules)
        assertEquals(emptyList<Boolean>(), rules.switches)
        assertEquals(emptyList<Pair<String, Throwable?>>(), logs)
    }

    @Test
    fun `an error at the reset does not escape`() {
        rules.ruleId = RULE_ID
        val failure = SecurityException("no access")
        rules.failure = failure
        quiet.reset()
        assertEquals(Constants.Messages.QUIET_OFF_FAILED, logs.single().first)
        assertSame(failure, logs.single().second)
    }

    private companion object {
        const val RULE_ID = "rule-1"
    }
}
