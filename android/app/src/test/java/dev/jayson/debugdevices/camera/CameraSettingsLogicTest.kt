package dev.jayson.debugdevices.camera

import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Test

class CameraSettingsLogicTest {
    @Test
    fun `the body needs at least one field`() {
        val error = assertThrows(ApiException::class.java) { CameraSettingsLogic.validate(CameraSettingsRequest()) }
        assertEquals(ErrorCode.BAD_REQUEST, error.code)
        CameraSettingsLogic.validate(CameraSettingsRequest(inSensorZoom = true))
        CameraSettingsLogic.validate(CameraSettingsRequest(afMode = AfMode.MACRO))
        CameraSettingsLogic.validate(CameraSettingsRequest(inSensorZoom = false, afMode = AfMode.CONTINUOUS))
    }

    @Test
    fun `macro only when the camera has it`() {
        assertEquals(AfMode.MACRO, CameraSettingsLogic.effectiveAfMode(AfMode.MACRO, macroSupported = true))
        assertEquals(AfMode.CONTINUOUS, CameraSettingsLogic.effectiveAfMode(AfMode.MACRO, macroSupported = false))
        assertEquals(AfMode.CONTINUOUS, CameraSettingsLogic.effectiveAfMode(AfMode.CONTINUOUS, macroSupported = true))
    }

    @Test
    fun `api words`() {
        assertEquals("\"macro\"", ApiJson.encodeToString(AfMode.serializer(), AfMode.MACRO))
        assertEquals("\"continuous\"", ApiJson.encodeToString(AfMode.serializer(), AfMode.CONTINUOUS))
    }
}
