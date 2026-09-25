package dev.jayson.debugdevices.camera

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class InSensorZoomLogicTest {
    private val key = Constants.InSensorZoom.VENDOR_KEY
    private val other = "android.control.aeMode"
    private val none = VendorKeyPresence(inSessionKeys = false, inRequestKeys = false)
    private val sessionOnly = VendorKeyPresence(inSessionKeys = true, inRequestKeys = false)

    @Test
    fun `vendor key name and value`() {
        assertEquals("org.codeaurora.qcamera3.sessionParameters.EnableInsensorZoom", key)
        assertEquals(1, Constants.InSensorZoom.ENABLED_VALUE)
        assertEquals("in_sensor_zoom", Constants.InSensorZoom.INTENT_EXTRA)
    }

    @Test
    fun `off by default and the intent extra wins when present`() {
        assertFalse(InSensorZoomLogic.requested(current = false, extraPresent = false, extraValue = false))
        assertTrue(InSensorZoomLogic.requested(current = false, extraPresent = true, extraValue = true))
        assertFalse(InSensorZoomLogic.requested(current = true, extraPresent = true, extraValue = false))
        assertTrue(InSensorZoomLogic.requested(current = true, extraPresent = false, extraValue = false))
    }

    @Test
    fun `presence looks in both key lists`() {
        assertEquals(sessionOnly, InSensorZoomLogic.presence(listOf(other, key), listOf(other)))
        assertEquals(VendorKeyPresence(false, true), InSensorZoomLogic.presence(listOf(other), listOf(key)))
        assertEquals(none, InSensorZoomLogic.presence(emptyList(), listOf(other)))
        assertTrue(sessionOnly.isAvailable)
        assertFalse(none.isAvailable)
    }

    @Test
    fun `plan`() {
        assertEquals(InSensorZoomState.OFF, InSensorZoomLogic.plan(false, sessionOnly, sessionTypeSupported = true))
        assertEquals(InSensorZoomState.UNSUPPORTED, InSensorZoomLogic.plan(true, none, sessionTypeSupported = true))
        assertEquals(
            InSensorZoomState.UNSUPPORTED,
            InSensorZoomLogic.plan(true, sessionOnly, sessionTypeSupported = false)
        )
        assertEquals(InSensorZoomState.ON, InSensorZoomLogic.plan(true, sessionOnly, sessionTypeSupported = true))
    }

    @Test
    fun `only ON sets the vendor parameters, with the photo app module`() {
        assertEquals(
            mapOf(key to 1, "xiaomi.app.module" to 163),
            InSensorZoomLogic.vendorParameters(InSensorZoomState.ON)
        )
        InSensorZoomState.entries.filter { it != InSensorZoomState.ON }.forEach {
            assertEquals(emptyMap<String, Int>(), InSensorZoomLogic.vendorParameters(it))
        }
    }

    @Test
    fun `a jump over the half-field range goes through the entry ratio in an ON session`() {
        val on = InSensorZoomState.ON
        assertEquals(listOf(3f, 5f), InSensorZoomLogic.zoomPath(on, from = 1f, to = 5f))
        assertEquals(listOf(3f, 4f), InSensorZoomLogic.zoomPath(on, from = 1.9f, to = 4f))
        assertEquals(listOf(5f), InSensorZoomLogic.zoomPath(on, from = 2f, to = 5f))
        assertEquals(listOf(6f), InSensorZoomLogic.zoomPath(on, from = 4f, to = 6f))
        assertEquals(listOf(3.9f), InSensorZoomLogic.zoomPath(on, from = 1f, to = 3.9f))
        assertEquals(listOf(1f), InSensorZoomLogic.zoomPath(on, from = 5f, to = 1f))
        InSensorZoomState.entries.filter { it != on }.forEach {
            assertEquals(listOf(5f), InSensorZoomLogic.zoomPath(it, from = 1f, to = 5f))
        }
        val entry = Constants.InSensorZoom.ENTRY_RATIO
        assertTrue(
            entry >= Constants.InSensorZoom.HALF_FIELD_RATIO && entry < Constants.InSensorZoom.QUARTER_FIELD_RATIO
        )
    }

    @Test
    fun `reconfigure on a change, or on true again after a fallback`() {
        assertTrue(InSensorZoomLogic.needsReconfigure(requested = false, InSensorZoomState.OFF, enabled = true))
        assertTrue(InSensorZoomLogic.needsReconfigure(requested = true, InSensorZoomState.ON, enabled = false))
        assertFalse(InSensorZoomLogic.needsReconfigure(requested = true, InSensorZoomState.ON, enabled = true))
        assertFalse(InSensorZoomLogic.needsReconfigure(requested = false, InSensorZoomState.OFF, enabled = false))
        assertTrue(InSensorZoomLogic.needsReconfigure(requested = true, InSensorZoomState.FALLBACK, enabled = true))
        assertFalse(InSensorZoomLogic.needsReconfigure(requested = true, InSensorZoomState.UNSUPPORTED, enabled = true))
    }

    @Test
    fun `state words for the API`() {
        assertEquals("\"fallback\"", ApiJson.encodeToString(InSensorZoomState.serializer(), InSensorZoomState.FALLBACK))
        assertEquals(
            "\"unsupported\"",
            ApiJson.encodeToString(InSensorZoomState.serializer(), InSensorZoomState.UNSUPPORTED)
        )
    }

    @Test
    fun `only ON uses the vendor session type`() {
        assertEquals(0x9005, Constants.InSensorZoom.VENDOR_SESSION_TYPE)
        assertEquals(36869, InSensorZoomLogic.sessionType(InSensorZoomState.ON))
        InSensorZoomState.entries.filter { it != InSensorZoomState.ON }.forEach {
            assertEquals(null, InSensorZoomLogic.sessionType(it))
        }
    }

    @Test
    fun `a failed vendor bind falls back to NORMAL, other states stay`() {
        assertEquals(InSensorZoomState.FALLBACK, InSensorZoomLogic.afterBindFailure(InSensorZoomState.ON))
        assertEquals(InSensorZoomState.OFF, InSensorZoomLogic.afterBindFailure(InSensorZoomState.OFF))
        assertEquals(InSensorZoomState.UNSUPPORTED, InSensorZoomLogic.afterBindFailure(InSensorZoomState.UNSUPPORTED))
        assertEquals(null, InSensorZoomLogic.sessionType(InSensorZoomLogic.afterBindFailure(InSensorZoomState.ON)))
    }

    @Test
    fun `only ON sets the vendor parameter`() {
        InSensorZoomState.entries.forEach {
            assertEquals(it == InSensorZoomState.ON, InSensorZoomLogic.setsVendorParameter(it))
        }
    }

    @Test
    fun `a session is stable when it streams without a camera error`() {
        assertTrue(InSensorZoomLogic.isStable(setOf(SessionEvent.PREVIEW_STREAMING)))
        assertFalse(InSensorZoomLogic.isStable(emptySet()))
        assertFalse(InSensorZoomLogic.isStable(setOf(SessionEvent.CAMERA_ERROR)))
        assertFalse(InSensorZoomLogic.isStable(setOf(SessionEvent.PREVIEW_STREAMING, SessionEvent.CAMERA_ERROR)))
    }

    @Test
    fun `an unstable ON session falls back, other states stay`() {
        assertEquals(
            InSensorZoomState.FALLBACK,
            InSensorZoomLogic.afterSessionCheck(InSensorZoomState.ON, stable = false)
        )
        assertEquals(InSensorZoomState.ON, InSensorZoomLogic.afterSessionCheck(InSensorZoomState.ON, stable = true))
        assertEquals(InSensorZoomState.OFF, InSensorZoomLogic.afterSessionCheck(InSensorZoomState.OFF, stable = false))
        assertEquals(
            InSensorZoomState.UNSUPPORTED,
            InSensorZoomLogic.afterSessionCheck(InSensorZoomState.UNSUPPORTED, stable = false)
        )
        assertFalse(InSensorZoomLogic.setsVendorParameter(InSensorZoomState.FALLBACK))
    }
}
