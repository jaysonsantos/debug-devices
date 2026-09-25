package dev.jayson.debugdevices.camera

import android.hardware.camera2.CameraMetadata
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class FocusLogicTest {
    private val approximate = FocusStatic(CameraMetadata.LENS_INFO_FOCUS_DISTANCE_CALIBRATION_APPROXIMATE, 10f)
    private val fixed = FocusStatic(CameraMetadata.LENS_INFO_FOCUS_DISTANCE_CALIBRATION_UNCALIBRATED, 0f)

    @Test
    fun `af state map`() {
        val expected = mapOf(
            CameraMetadata.CONTROL_AF_STATE_FOCUSED_LOCKED to FocusState.FOCUSED,
            CameraMetadata.CONTROL_AF_STATE_PASSIVE_FOCUSED to FocusState.FOCUSED,
            CameraMetadata.CONTROL_AF_STATE_ACTIVE_SCAN to FocusState.SCANNING,
            CameraMetadata.CONTROL_AF_STATE_PASSIVE_SCAN to FocusState.SCANNING,
            CameraMetadata.CONTROL_AF_STATE_NOT_FOCUSED_LOCKED to FocusState.UNFOCUSED,
            CameraMetadata.CONTROL_AF_STATE_PASSIVE_UNFOCUSED to FocusState.UNFOCUSED,
            CameraMetadata.CONTROL_AF_STATE_INACTIVE to FocusState.UNKNOWN
        )
        expected.forEach { (af, state) -> assertEquals("af $af", state, FocusLogic.state(af)) }
        assertEquals(FocusState.UNKNOWN, FocusLogic.state(null))
        assertEquals(FocusState.UNKNOWN, FocusLogic.state(99))
    }

    @Test
    fun `calibration map`() {
        assertEquals(
            FocusCalibration.UNCALIBRATED,
            FocusLogic.calibration(CameraMetadata.LENS_INFO_FOCUS_DISTANCE_CALIBRATION_UNCALIBRATED)
        )
        assertEquals(
            FocusCalibration.APPROXIMATE,
            FocusLogic.calibration(CameraMetadata.LENS_INFO_FOCUS_DISTANCE_CALIBRATION_APPROXIMATE)
        )
        assertEquals(
            FocusCalibration.CALIBRATED,
            FocusLogic.calibration(CameraMetadata.LENS_INFO_FOCUS_DISTANCE_CALIBRATION_CALIBRATED)
        )
        assertEquals(FocusCalibration.UNCALIBRATED, FocusLogic.calibration(null))
    }

    @Test
    fun `null before bind, null distance before the first result`() {
        assertNull(FocusLogic.info(static = null, sample = FocusSample(3f, null)))
        val noResult = FocusLogic.info(approximate, sample = null)!!
        assertNull(noResult.distanceDiopters)
        assertEquals(FocusState.UNKNOWN, noResult.state)
        assertEquals(FocusCalibration.APPROXIMATE, noResult.calibration)
        assertEquals(10f, noResult.minDistanceDiopters)
    }

    @Test
    fun `distance from the latest result, null with fixed focus`() {
        val sample = FocusSample(3.41f, CameraMetadata.CONTROL_AF_STATE_PASSIVE_FOCUSED)
        val info = FocusLogic.info(approximate, sample)!!
        assertEquals(3.41f, info.distanceDiopters)
        assertEquals(FocusState.FOCUSED, info.state)
        assertNull(FocusLogic.info(fixed, sample)!!.distanceDiopters)
        assertEquals(0f, FocusLogic.info(FocusStatic(null, null), sample)!!.minDistanceDiopters)
        assertEquals(0f, FocusLogic.info(approximate, FocusSample(0f, null))!!.distanceDiopters)
    }

    @Test
    fun `label distance in cm only with real units`() {
        val info = FocusLogic.info(approximate, FocusSample(3.41f, null))
        assertEquals(29, FocusLogic.distanceCm(info))
        assertEquals(10, FocusLogic.distanceCm(FocusLogic.info(approximate, FocusSample(10f, null))))
        val uncalibrated = FocusStatic(CameraMetadata.LENS_INFO_FOCUS_DISTANCE_CALIBRATION_UNCALIBRATED, 10f)
        assertNull(FocusLogic.distanceCm(FocusLogic.info(uncalibrated, FocusSample(3.41f, null))))
        assertNull(FocusLogic.distanceCm(FocusLogic.info(approximate, FocusSample(0f, null))))
        assertNull(FocusLogic.distanceCm(FocusLogic.info(approximate, null)))
        assertNull(FocusLogic.distanceCm(null))
    }

    @Test
    fun `output width is the long side`() {
        assertEquals(4080, FocusLogic.outputWidthPx(4080, 3060))
        assertEquals(4080, FocusLogic.outputWidthPx(3060, 4080))
    }
}
