package dev.jayson.debugdevices.camera

import io.ktor.client.request.delete
import io.ktor.client.request.get
import io.ktor.client.request.post
import io.ktor.client.request.setBody
import io.ktor.client.statement.HttpResponse
import io.ktor.client.statement.bodyAsText
import io.ktor.client.statement.readRawBytes
import io.ktor.http.ContentType
import io.ktor.http.HttpStatusCode
import io.ktor.http.contentType
import io.ktor.server.testing.ApplicationTestBuilder
import io.ktor.server.testing.testApplication
import java.net.HttpURLConnection
import java.net.ServerSocket
import java.net.URI
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import kotlin.coroutines.cancellation.CancellationException
import kotlin.system.measureTimeMillis
import kotlinx.coroutines.asCoroutineDispatcher
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.delay
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withContext
import kotlinx.coroutines.yield
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class ApiServerTest {
    /** Uses a real [ControlGate], like the CameraX controller. [gate] is ready unless a test says otherwise. */
    private class FakeCamera(var status: CameraStatus?, val gate: ControlGate = ControlGate()) : CameraPort {
        /** Like the real camera: `unsupported` without the vendor keys. */
        var vendorSupported = true
        var rebinds = 0
        var focusTarget: FocusTarget? = null
        var overlay: List<OverlayBox> = emptyList()

        var arrows: List<OverlayArrow> = emptyList()
        var macroSupported = true

        override suspend fun setOverlay(boxes: List<OverlayBox>, arrows: List<OverlayArrow>): CameraStatus =
            gate.control {
                overlay = boxes
                this.arrows = arrows
                status().copy(overlayBoxes = boxes.size, overlayArrows = arrows.size).also { status = it }
            }

        override suspend fun setOverlayVisible(visible: Boolean): CameraStatus = gate.control {
            status().copy(overlayVisible = visible).also { status = it }
        }

        override suspend fun setCameraSettings(inSensorZoom: Boolean?, afMode: AfMode?): CameraStatus {
            inSensorZoom?.let { setInSensorZoom(it) }
            return gate.control {
                val current = status()
                val mode = afMode?.let { CameraSettingsLogic.effectiveAfMode(it, macroSupported) } ?: current.afMode
                current.copy(afMode = mode).also { status = it }
            }
        }

        override suspend fun focusAt(target: FocusTarget): CameraStatus = gate.control {
            // Like the real camera: the lower tenth of the screen is outside the preview.
            if (target is FocusTarget.Screen && target.y > OUTSIDE_PREVIEW_Y) {
                throw ApiException(ErrorCode.BAD_REQUEST, Constants.Messages.FOCUS_OUTSIDE_PREVIEW)
            }
            focusTarget = target
            status().let { it.copy(focus = it.focus?.copy(state = FocusState.SCANNING)) }.also { status = it }
        }

        override suspend fun setInSensorZoom(enabled: Boolean): CameraStatus = gate.control {
            val current = status()
            val state = when {
                !enabled -> InSensorZoomState.OFF
                !vendorSupported -> InSensorZoomState.UNSUPPORTED
                else -> InSensorZoomState.ON
            }
            if (state != current.inSensorZoom) rebinds++
            // The rebind keeps the zoom and the torch.
            current.copy(inSensorZoom = state).also { status = it }
        }

        val jpeg = byteArrayOf(0xFF.toByte(), 0xD8.toByte(), 0xFF.toByte(), 0xD9.toByte())
        var captureError: Exception? = null
        var statusError: Exception? = null

        override suspend fun status(): CameraStatus {
            statusError?.let { throw it }
            gate.checkReady()
            return status ?: throw ApiException(ErrorCode.CAMERA_NOT_READY, Constants.Messages.CAMERA_NOT_READY)
        }

        override suspend fun updateZoom(target: (CameraStatus) -> Float): CameraStatus = gate.control {
            val current = status()
            // A suspension point between the read and the write, like the CameraX future.
            yield()
            current.copy(zoomRatio = target(current)).also { status = it }
        }

        override suspend fun setTorch(enabled: Boolean): CameraStatus =
            gate.control { status().copy(torchEnabled = enabled).also { status = it } }

        /** Runs inside the rotation request before its status read, like the layout wait of the real camera. */
        var beforeRotationRead: suspend () -> Unit = {}

        override suspend fun setRotation(lockedRotation: Int?): CameraStatus = gate.control {
            beforeRotationRead()
            val degrees = OrientationLogic.surfaceDegrees(lockedRotation ?: SENSOR_ROTATION)
            status().copy(rotationDegrees = degrees, rotationLocked = lockedRotation != null).also { status = it }
        }

        override suspend fun setPreviewFlip(flip: PreviewFlip): CameraStatus = gate.control {
            status().copy(previewFlipHorizontal = flip.horizontal, previewFlipVertical = flip.vertical)
                .also { status = it }
        }

        override suspend fun capture(): Snapshot {
            captureError?.let { throw it }
            val current = status()
            return Snapshot(jpeg, current.rotationDegrees, current.appStartId)
        }
    }

    private val ready = CameraStatus(
        zoomRatio = 1f,
        minZoomRatio = 1f,
        maxZoomRatio = 8f,
        torchEnabled = false,
        hasFlashUnit = true,
        rotationDegrees = 0,
        rotationLocked = false,
        previewFlipHorizontal = false,
        previewFlipVertical = false,
        focus = FocusInfo(
            distanceDiopters = 3.5f,
            state = FocusState.FOCUSED,
            calibration = FocusCalibration.APPROXIMATE,
            minDistanceDiopters = 10f
        ),
        optics = Optics(focalLengthMm = 6.07f, sensorWidthMm = 9.14f, outputWidthPx = 4080),
        inSensorZoom = InSensorZoomState.OFF,
        overlayBoxes = 0,
        overlayArrows = 0,
        afMode = AfMode.CONTINUOUS,
        appStartId = "0192f3a4-5b6c-7d8e-9f00-112233445566",
        previewRegion = PreviewRegion(snapshotX = 0.2f, snapshotY = 0f, width = 0.6f, height = 1f),
        overlayRegion = PreviewRegion(snapshotX = 0.2f, snapshotY = 0.04f, width = 0.6f, height = 0.9f),
        overlayVisible = true
    )

    private val unexpected = mutableListOf<Throwable>()

    private fun ready(status: CameraStatus?) = FakeCamera(status).apply { runBlocking { gate.start {} } }

    private fun api(camera: FakeCamera, block: suspend ApplicationTestBuilder.() -> Unit) = testApplication {
        application { cameraApi(camera, APP_VERSION) { unexpected += it } }
        block()
    }

    private suspend fun ApplicationTestBuilder.postJson(path: String, body: String): HttpResponse = client.post(path) {
        contentType(ContentType.Application.Json)
        setBody(body)
    }

    private suspend fun HttpResponse.status(): CameraStatus = ApiJson.decodeFromString(bodyAsText())

    private suspend fun HttpResponse.error(): ApiError = ApiJson.decodeFromString(bodyAsText())

    @Test
    fun `health returns ok and the version`() = api(ready(null)) {
        val response = client.get(Constants.Paths.HEALTH)
        assertEquals(HttpStatusCode.OK, response.status)
        assertEquals("""{"ok":true,"app_version":"0.1.0"}""", response.bodyAsText())
    }

    @Test
    fun `status uses snake case`() = api(ready(ready)) {
        val body = client.get(Constants.Paths.STATUS).bodyAsText()
        assertEquals(
            """{"zoom_ratio":1.0,"min_zoom_ratio":1.0,"max_zoom_ratio":8.0,"torch_enabled":false,"has_flash_unit":true,"rotation_degrees":0,"rotation_locked":false,"preview_flip_horizontal":false,"preview_flip_vertical":false,"focus":{"distance_diopters":3.5,"state":"focused","calibration":"approximate","min_distance_diopters":10.0},"optics":{"focal_length_mm":6.07,"sensor_width_mm":9.14,"output_width_px":4080},"in_sensor_zoom":"off","overlay_boxes":0,"overlay_arrows":0,"af_mode":"continuous","app_start_id":"0192f3a4-5b6c-7d8e-9f00-112233445566","preview_region":{"snapshot_x":0.2,"snapshot_y":0.0,"width":0.6,"height":1.0},"overlay_region":{"snapshot_x":0.2,"snapshot_y":0.04,"width":0.6,"height":0.9},"overlay_visible":true}""",
            body
        )
    }

    @Test
    fun `status before bind is 503 camera_not_ready`() = api(ready(null)) {
        val response = client.get(Constants.Paths.STATUS)
        assertEquals(HttpStatusCode.ServiceUnavailable, response.status)
        assertEquals(ErrorCode.CAMERA_NOT_READY, response.error().error)
        assertEquals("""{"error":"camera_not_ready","message":"Camera is not bound yet"}""", response.bodyAsText())
    }

    @Test
    fun `zoom step in and out`() = api(ready(ready.copy(zoomRatio = 2f))) {
        assertEquals(3f, postJson(Constants.Paths.ZOOM, """{"step":"in"}""").status().zoomRatio)
        assertEquals(2f, postJson(Constants.Paths.ZOOM, """{"step":"out"}""").status().zoomRatio)
    }

    @Test
    fun `zoom ratio is clamped`() = api(ready(ready)) {
        assertEquals(8f, postJson(Constants.Paths.ZOOM, """{"ratio":20}""").status().zoomRatio)
        assertEquals(1f, postJson(Constants.Paths.ZOOM, """{"ratio":0.2}""").status().zoomRatio)
        assertEquals(2.5f, postJson(Constants.Paths.ZOOM, """{"ratio":2.5}""").status().zoomRatio)
    }

    @Test
    fun `zoom with both fields is 400`() = api(ready(ready)) {
        val response = postJson(Constants.Paths.ZOOM, """{"ratio":2,"step":"in"}""")
        assertEquals(HttpStatusCode.BadRequest, response.status)
        assertEquals(ErrorCode.BAD_REQUEST, response.error().error)
    }

    @Test
    fun `zoom with a bad step is 400`() = api(ready(ready)) {
        val response = postJson(Constants.Paths.ZOOM, """{"step":"sideways"}""")
        assertEquals(HttpStatusCode.BadRequest, response.status)
        assertEquals(ErrorCode.BAD_REQUEST, response.error().error)
    }

    @Test
    fun `zoom with broken json is 400`() = api(ready(ready)) {
        val response = postJson(Constants.Paths.ZOOM, "{")
        assertEquals(HttpStatusCode.BadRequest, response.status)
        assertEquals(ErrorCode.BAD_REQUEST, response.error().error)
    }

    @Test
    fun `zoom without content type is 400`() = api(ready(ready)) {
        val response = client.post(Constants.Paths.ZOOM) { setBody("""{"ratio":2}""") }
        assertEquals(HttpStatusCode.BadRequest, response.status)
        assertEquals(ErrorCode.BAD_REQUEST, response.error().error)
    }

    @Test
    fun `torch on and off`() = api(ready(ready)) {
        assertEquals(true, postJson(Constants.Paths.TORCH, """{"enabled":true}""").status().torchEnabled)
        assertEquals(false, postJson(Constants.Paths.TORCH, """{"enabled":false}""").status().torchEnabled)
    }

    @Test
    fun `torch without flash unit is 409`() = api(ready(ready.copy(hasFlashUnit = false))) {
        val response = postJson(Constants.Paths.TORCH, """{"enabled":true}""")
        assertEquals(HttpStatusCode.Conflict, response.status)
        assertEquals(ErrorCode.NO_FLASH_UNIT, response.error().error)
    }

    @Test
    fun `snapshot returns jpeg bytes`() {
        val camera = ready(ready)
        api(camera) {
            val response = client.get(Constants.Paths.SNAPSHOT)
            assertEquals(HttpStatusCode.OK, response.status)
            assertEquals(ContentType.Image.JPEG, response.contentType()?.withoutParameters())
            assertArrayEquals(camera.jpeg, response.readRawBytes())
        }
    }

    @Test
    fun `snapshot failure is 500 capture_failed`() {
        val camera = ready(ready).apply { captureError = ApiException(ErrorCode.CAPTURE_FAILED, "boom") }
        api(camera) {
            val response = client.get(Constants.Paths.SNAPSHOT)
            assertEquals(HttpStatusCode.InternalServerError, response.status)
            assertEquals(ErrorCode.CAPTURE_FAILED, response.error().error)
        }
    }

    @Test
    fun `health is 200 while the camera is not bound`() = api(ready(null)) {
        assertEquals(HttpStatusCode.OK, client.get(Constants.Paths.HEALTH).status)
        assertEquals(HttpStatusCode.ServiceUnavailable, client.get(Constants.Paths.SNAPSHOT).status)
        assertEquals(HttpStatusCode.ServiceUnavailable, postJson(Constants.Paths.ZOOM, """{"step":"in"}""").status)
        assertEquals(HttpStatusCode.ServiceUnavailable, postJson(Constants.Paths.TORCH, """{"enabled":true}""").status)
    }

    @Test
    fun `zoom with neither field is 400`() = api(ready(ready)) {
        val response = postJson(Constants.Paths.ZOOM, "{}")
        assertEquals(HttpStatusCode.BadRequest, response.status)
        assertEquals(ErrorCode.BAD_REQUEST, response.error().error)
    }

    @Test
    fun `wrong method on a known path is 405 method_not_allowed`() = api(ready(ready)) {
        val responses = listOf(
            client.get(Constants.Paths.ZOOM),
            client.get(Constants.Paths.TORCH),
            client.post(Constants.Paths.STATUS),
            client.delete(Constants.Paths.SNAPSHOT)
        )
        for (response in responses) {
            assertEquals(HttpStatusCode.MethodNotAllowed, response.status)
            assertEquals(
                """{"error":"method_not_allowed","message":"This method is not allowed on this endpoint"}""",
                response.bodyAsText()
            )
        }
    }

    @Test
    fun `snapshot keeps the torch state`() {
        val camera = ready(ready.copy(torchEnabled = true))
        api(camera) {
            assertEquals(HttpStatusCode.OK, client.get(Constants.Paths.SNAPSHOT).status)
            assertEquals(true, client.get(Constants.Paths.STATUS).status().torchEnabled)
        }
    }

    @Test
    fun `zoom during the start state is 503 and works after it`() {
        val camera = FakeCamera(ready)
        api(camera) {
            val response = postJson(Constants.Paths.ZOOM, """{"ratio":3}""")
            assertEquals(HttpStatusCode.ServiceUnavailable, response.status)
            assertEquals(ErrorCode.CAMERA_NOT_READY, response.error().error)
            assertEquals(HttpStatusCode.ServiceUnavailable, client.get(Constants.Paths.STATUS).status)
            assertEquals(HttpStatusCode.OK, client.get(Constants.Paths.HEALTH).status)
            camera.gate.start {}
            assertEquals(3f, postJson(Constants.Paths.ZOOM, """{"ratio":3}""").status().zoomRatio)
        }
    }

    @Test
    fun `ratio as a string is 400`() = api(ready(ready)) {
        for (body in listOf("""{"ratio":"2"}""", """{"ratio":"abc"}""", """{"ratio":true}""", """{"ratio":[2]}""")) {
            val response = postJson(Constants.Paths.ZOOM, body)
            assertEquals(body, HttpStatusCode.BadRequest, response.status)
            assertEquals(body, ErrorCode.BAD_REQUEST, response.error().error)
        }
        assertEquals(1f, client.get(Constants.Paths.STATUS).status().zoomRatio)
    }

    @Test
    fun `ratio as a number still works`() = api(ready(ready)) {
        assertEquals(2f, postJson(Constants.Paths.ZOOM, """{"ratio":2}""").status().zoomRatio)
        assertEquals(2.5f, postJson(Constants.Paths.ZOOM, """{"ratio":2.5e0}""").status().zoomRatio)
        assertEquals(2.5f / 1.5f, postJson(Constants.Paths.ZOOM, """{"ratio":null,"step":"out"}""").status().zoomRatio)
    }

    @Test
    fun `ratio out of float range is 400`() = api(ready(ready)) {
        assertEquals(HttpStatusCode.BadRequest, postJson(Constants.Paths.ZOOM, """{"ratio":1e400}""").status)
    }

    @Test
    fun `enabled as a string is 400`() = api(ready(ready)) {
        for (body in listOf("""{"enabled":"true"}""", """{"enabled":1}""", """{}""")) {
            val response = postJson(Constants.Paths.TORCH, body)
            assertEquals(body, HttpStatusCode.BadRequest, response.status)
            assertEquals(body, ErrorCode.BAD_REQUEST, response.error().error)
        }
    }

    @Test
    fun `concurrent zoom requests all succeed`() = api(ready(ready)) {
        val responses = coroutineScope {
            (1..CONCURRENT_REQUESTS).map { async { postJson(Constants.Paths.ZOOM, """{"step":"in"}""") } }.awaitAll()
        }
        responses.forEach { assertEquals(HttpStatusCode.OK, it.status) }
        // 1.5^10 is above the max, so every step counted and the last ones clamp to 8.
        assertEquals(8f, client.get(Constants.Paths.STATUS).status().zoomRatio)
    }

    @Test
    fun `unexpected error is 500 internal_error and is logged`() {
        val camera = ready(ready).apply { statusError = IllegalStateException("bug") }
        api(camera) {
            val response = client.get(Constants.Paths.STATUS)
            assertEquals(HttpStatusCode.InternalServerError, response.status)
            assertEquals(ErrorCode.INTERNAL_ERROR, response.error().error)
            assertEquals(1, unexpected.size)
            assertEquals("bug", unexpected.single().message)
        }
    }

    @Test
    fun `contract errors are not logged as unexpected`() = api(ready(null)) {
        assertEquals(HttpStatusCode.ServiceUnavailable, client.get(Constants.Paths.STATUS).status)
        assertEquals(HttpStatusCode.NotFound, client.get("/v1/nope").status)
        assertEquals(0, unexpected.size)
    }

    @Test
    fun `rotation locks and unlocks`() = api(ready(ready)) {
        val locked = postJson(Constants.Paths.ROTATION, """{"degrees":90}""").status()
        assertEquals(90, locked.rotationDegrees)
        assertEquals(true, locked.rotationLocked)
        assertEquals(true, client.get(Constants.Paths.STATUS).status().rotationLocked)
        val auto = postJson(Constants.Paths.ROTATION, """{"auto":true}""").status()
        assertEquals(0, auto.rotationDegrees)
        assertEquals(false, auto.rotationLocked)
    }

    @Test
    fun `rotation bad bodies are 400`() = api(ready(ready)) {
        val bodies = listOf(
            "{}",
            """{"degrees":90,"auto":true}""",
            """{"degrees":45}""",
            """{"degrees":360}""",
            """{"degrees":"90"}""",
            """{"degrees":90.5}""",
            """{"auto":false}""",
            """{"auto":"true"}"""
        )
        for (body in bodies) {
            val response = postJson(Constants.Paths.ROTATION, body)
            assertEquals(body, HttpStatusCode.BadRequest, response.status)
            assertEquals(body, ErrorCode.BAD_REQUEST, response.error().error)
        }
    }

    @Test
    fun `preview region can be null`() = api(ready(ready.copy(previewRegion = null, overlayRegion = null))) {
        assertTrue(client.get(Constants.Paths.STATUS).bodyAsText().contains(""""preview_region":null"""))
        assertTrue(client.get(Constants.Paths.STATUS).bodyAsText().contains(""""overlay_region":null"""))
    }

    @Test
    fun `focus is null before the camera is bound, and the distance can be null`() {
        api(ready(ready.copy(focus = null))) {
            val body = client.get(Constants.Paths.STATUS).bodyAsText()
            assertTrue(body, body.contains(""""focus":null"""))
            assertTrue(body, body.contains(""""optics":{"focal_length_mm":6.07"""))
        }
        val noDistance = ready.copy(focus = ready.focus?.copy(distanceDiopters = null, state = FocusState.UNKNOWN))
        api(ready(noDistance)) {
            val body = client.get(Constants.Paths.STATUS).bodyAsText()
            assertTrue(body, body.contains(""""focus":{"distance_diopters":null,"state":"unknown""""))
            assertEquals(null, ApiJson.decodeFromString<CameraStatus>(body).focus?.distanceDiopters)
        }
    }

    @Test
    fun `overlay sets and clears boxes`() {
        val camera = ready(ready)
        api(camera) {
            val body =
                """{"boxes":[{"snapshot_x":0.42,"snapshot_y":0.31,"width":0.05,"height":0.04,"label":"U730"},""" +
                    """{"snapshot_x":0,"snapshot_y":0,"width":1,"height":1,"label":""}]}"""
            val set = postJson(Constants.Paths.OVERLAY, body)
            assertEquals(HttpStatusCode.OK, set.status)
            assertEquals(2, set.status().overlayBoxes)
            assertEquals("U730", camera.overlay.first().label)
            assertTrue(client.get(Constants.Paths.STATUS).bodyAsText().contains(""""overlay_boxes":2"""))
            assertEquals(0, postJson(Constants.Paths.OVERLAY, """{"boxes":[]}""").status().overlayBoxes)
        }
    }

    @Test
    fun `overlay bad bodies are 400`() = api(ready(ready)) {
        fun box(x: String = "0.1", y: String = "0.1", w: String = "0.2", h: String = "0.2", label: String = "\"R1\"") =
            """{"snapshot_x":$x,"snapshot_y":$y,"width":$w,"height":$h,"label":$label}"""
        val nine = (1..9).joinToString(",") { box() }
        val bodies = listOf(
            "{}",
            """{"boxes":null}""",
            """{"boxes":[$nine]}""",
            """{"boxes":[${box(w = "0")}]}""",
            """{"boxes":[${box(h = "-0.1")}]}""",
            """{"boxes":[${box(x = "0.9", w = "0.2")}]}""",
            """{"boxes":[${box(y = "-0.01")}]}""",
            """{"boxes":[${box(x = "\"0.1\"")}]}""",
            """{"boxes":[${box(label = "\"" + "x".repeat(33) + "\"")}]}""",
            """{"boxes":[${box(label = "5")}]}""",
            """{"boxes":[{"snapshot_x":0.1,"snapshot_y":0.1,"width":0.2,"height":0.2}]}""",
            """{"boxes":[],"color":"red"}"""
        )
        for (body in bodies) {
            val response = postJson(Constants.Paths.OVERLAY, body)
            assertEquals(body, HttpStatusCode.BadRequest, response.status)
            assertEquals(body, ErrorCode.BAD_REQUEST, response.error().error)
        }
        assertEquals(
            HttpStatusCode.OK,
            postJson(
                Constants.Paths.OVERLAY,
                """{"boxes":[${box(
                    label =
                        "\"" + "x".repeat(32) + "\""
                )}]}"""
            ).status
        )
        assertEquals(
            HttpStatusCode.OK,
            postJson(
                Constants.Paths.OVERLAY,
                """{"boxes":[${(1..8).joinToString(",") {
                    box()
                }}]}"""
            ).status
        )
    }

    @Test
    fun `overlay arrows set, count, and clear`() {
        val camera = ready(ready)
        api(camera) {
            val body = """{"boxes":[],"arrows":[{"angle_deg":45,"label":"J4 ~4 cm"},{"angle_deg":-90.5,"label":""}]}"""
            val set = postJson(Constants.Paths.OVERLAY, body).status()
            assertEquals(2, set.overlayArrows)
            assertEquals(45f, camera.arrows.first().angleDeg)
            // A body without arrows removes them.
            assertEquals(0, postJson(Constants.Paths.OVERLAY, """{"boxes":[]}""").status().overlayArrows)
        }
    }

    @Test
    fun `overlay bad arrows are 400`() = api(ready(ready)) {
        val five = (1..5).joinToString(",") { """{"angle_deg":0,"label":"a"}""" }
        val bodies = listOf(
            """{"boxes":[],"arrows":[$five]}""",
            """{"boxes":[],"arrows":[{"angle_deg":"45","label":"a"}]}""",
            """{"boxes":[],"arrows":[{"angle_deg":1e400,"label":"a"}]}""",
            """{"boxes":[],"arrows":[{"angle_deg":0,"label":"${"x".repeat(33)}"}]}""",
            """{"boxes":[],"arrows":[{"angle_deg":0}]}""",
            """{"boxes":[],"arrows":[{"angle_deg":0,"label":"a","color":"red"}]}""",
            """{"arrows":[]}"""
        )
        for (body in bodies) {
            val response = postJson(Constants.Paths.OVERLAY, body)
            assertEquals(body, HttpStatusCode.BadRequest, response.status)
            assertEquals(body, ErrorCode.BAD_REQUEST, response.error().error)
        }
        val four = (1..4).joinToString(",") { """{"angle_deg":720,"label":"a"}""" }
        assertEquals(HttpStatusCode.OK, postJson(Constants.Paths.OVERLAY, """{"boxes":[],"arrows":[$four]}""").status)
    }

    @Test
    fun `af mode macro and back, alone or with in-sensor zoom`() {
        val camera = ready(ready.copy(zoomRatio = 2f))
        api(camera) {
            val macro = postJson(Constants.Paths.CAMERA, """{"af_mode":"macro"}""").status()
            assertEquals(AfMode.MACRO, macro.afMode)
            assertEquals(InSensorZoomState.OFF, macro.inSensorZoom)
            assertTrue(client.get(Constants.Paths.STATUS).bodyAsText().contains(""""af_mode":"macro""""))
            val both = postJson(Constants.Paths.CAMERA, """{"in_sensor_zoom":true,"af_mode":"continuous"}""").status()
            assertEquals(AfMode.CONTINUOUS, both.afMode)
            assertEquals(InSensorZoomState.ON, both.inSensorZoom)
            assertEquals(2f, both.zoomRatio)
        }
    }

    @Test
    fun `af mode macro on a phone without it is 200 and stays continuous`() {
        val camera = ready(ready).apply { macroSupported = false }
        api(camera) {
            val response = postJson(Constants.Paths.CAMERA, """{"af_mode":"macro"}""")
            assertEquals(HttpStatusCode.OK, response.status)
            assertEquals(AfMode.CONTINUOUS, response.status().afMode)
        }
    }

    @Test
    fun `camera settings bad af mode is 400`() = api(ready(ready)) {
        for (body in listOf(
            """{"af_mode":"MACRO"}""",
            """{"af_mode":"auto"}""",
            """{"af_mode":1}""",
            """{"af_mode":null}"""
        )) {
            val response = postJson(Constants.Paths.CAMERA, body)
            assertEquals(body, HttpStatusCode.BadRequest, response.status)
        }
    }

    @Test
    fun `overlay visible hides and shows without changing the shapes`() {
        val camera = ready(ready)
        api(camera) {
            val shapes = """{"boxes":[{"snapshot_x":0.1,"snapshot_y":0.1,"width":0.2,"height":0.2,"label":"U1"}],""" +
                """"arrows":[{"angle_deg":0,"label":"J4"}]}"""
            postJson(Constants.Paths.OVERLAY, shapes)
            val hidden = postJson(Constants.Paths.OVERLAY, """{"visible":false}""").status()
            assertEquals(false, hidden.overlayVisible)
            assertEquals(1, hidden.overlayBoxes)
            assertEquals(1, hidden.overlayArrows)
            assertEquals(1, camera.overlay.size)
            // New shapes do not change the visibility.
            val replaced = postJson(Constants.Paths.OVERLAY, """{"boxes":[]}""").status()
            assertEquals(false, replaced.overlayVisible)
            assertEquals(0, replaced.overlayBoxes)
            val shown = postJson(Constants.Paths.OVERLAY, """{"visible":true}""").status()
            assertEquals(true, shown.overlayVisible)
            assertTrue(client.get(Constants.Paths.STATUS).bodyAsText().contains(""""overlay_visible":true"""))
        }
    }

    @Test
    fun `overlay visible bad bodies are 400`() = api(ready(ready)) {
        val bodies = listOf(
            "{}",
            """{"visible":"false"}""",
            """{"visible":0}""",
            """{"visible":false,"boxes":[]}""",
            """{"visible":true,"arrows":[]}""",
            """{"arrows":[]}""",
            """{"visible":null}"""
        )
        for (body in bodies) {
            val response = postJson(Constants.Paths.OVERLAY, body)
            assertEquals(body, HttpStatusCode.BadRequest, response.status)
            assertEquals(body, ErrorCode.BAD_REQUEST, response.error().error)
        }
    }

    @Test
    fun `snapshot has the rotation and app start id headers (C12)`() = api(ready(ready.copy(rotationDegrees = 90))) {
        val response = client.get(Constants.Paths.SNAPSHOT)
        assertEquals(HttpStatusCode.OK, response.status)
        assertEquals("90", response.headers[Constants.Snapshot.HEADER_ROTATION_DEGREES])
        assertEquals("0192f3a4-5b6c-7d8e-9f00-112233445566", response.headers[Constants.Snapshot.HEADER_APP_START_ID])
    }

    @Test
    fun `tags match the contract pattern, for boxes and arrows (C3, C7)`() {
        val camera = ready(ready)
        api(camera) {
            fun box(tag: String) =
                """{"boxes":[{"snapshot_x":0.1,"snapshot_y":0.1,"width":0.2,"height":0.2,"label":"R1","tag":$tag}]}"""
            fun arrow(tag: String) = """{"boxes":[],"arrows":[{"angle_deg":0,"label":"J4","tag":$tag}]}"""
            for (good in listOf("a", "U7", "R12", "9")) {
                assertEquals(good, HttpStatusCode.OK, postJson(Constants.Paths.OVERLAY, box("\"$good\"")).status)
                assertEquals(good, camera.overlay.single().tag)
                assertEquals(good, HttpStatusCode.OK, postJson(Constants.Paths.OVERLAY, arrow("\"$good\"")).status)
                assertEquals(good, camera.arrows.single().tag)
            }
            for (bad in listOf("\"\"", "\"ABCD\"", "\"Ü1\"", "\"A-1\"", "\"A \"", "7")) {
                assertEquals(bad, HttpStatusCode.BadRequest, postJson(Constants.Paths.OVERLAY, box(bad)).status)
                assertEquals(bad, HttpStatusCode.BadRequest, postJson(Constants.Paths.OVERLAY, arrow(bad)).status)
            }
        }
    }

    @Test
    fun `inside the image is strict at the top left, with a small tolerance at the right and bottom (C10)`() =
        api(ready(ready)) {
            fun box(x: String, y: String, w: String, h: String) =
                """{"boxes":[{"snapshot_x":$x,"snapshot_y":$y,"width":$w,"height":$h,"label":"R1"}]}"""
            assertEquals(
                HttpStatusCode.OK,
                postJson(Constants.Paths.OVERLAY, box("0.5", "0.5", "0.50009", "0.50009")).status
            )
            assertEquals(HttpStatusCode.OK, postJson(Constants.Paths.OVERLAY, box("0", "0", "1", "1")).status)
            assertEquals(
                HttpStatusCode.BadRequest,
                postJson(Constants.Paths.OVERLAY, box("0.5", "0.5", "0.5002", "0.1")).status
            )
            assertEquals(
                HttpStatusCode.BadRequest,
                postJson(Constants.Paths.OVERLAY, box("0.5", "0.5", "0.1", "0.5002")).status
            )
            assertEquals(
                HttpStatusCode.BadRequest,
                postJson(Constants.Paths.OVERLAY, box("-0.00001", "0.5", "0.1", "0.1")).status
            )
            assertEquals(
                HttpStatusCode.BadRequest,
                postJson(Constants.Paths.OVERLAY, box("0.5", "-0.00001", "0.1", "0.1")).status
            )
        }

    @Test
    fun `overlay body rules pinned by the contract (C1, C2, C4)`() {
        val camera = ready(ready)
        api(camera) {
            // C1: visible together with shapes is 400. C2: shapes need boxes.
            for (body in listOf(
                """{"visible":false,"boxes":[]}""",
                """{"visible":true,"arrows":[]}""",
                """{"arrows":[]}"""
            )) {
                assertEquals(body, HttpStatusCode.BadRequest, postJson(Constants.Paths.OVERLAY, body).status)
            }
            // C4: the counts are the kept boxes and arrows, also while hidden.
            val shapes = """{"boxes":[{"snapshot_x":0.1,"snapshot_y":0.1,"width":0.2,"height":0.2,"label":"R1"}],""" +
                """"arrows":[{"angle_deg":90,"label":"J4"}]}"""
            postJson(Constants.Paths.OVERLAY, shapes)
            val hidden = postJson(Constants.Paths.OVERLAY, """{"visible":false}""").status()
            assertEquals(1, hidden.overlayBoxes)
            assertEquals(1, hidden.overlayArrows)
        }
    }

    @Test
    fun `overlay tags are 1 to 3 characters`() {
        val camera = ready(ready)
        api(camera) {
            fun body(tag: String) =
                """{"boxes":[{"snapshot_x":0.1,"snapshot_y":0.1,"width":0.2,"height":0.2,"label":"R1",$tag}]}"""
            assertEquals(HttpStatusCode.OK, postJson(Constants.Paths.OVERLAY, body(""""tag":"U7"""")).status)
            assertEquals("U7", camera.overlay.single().tag)
            assertEquals(HttpStatusCode.OK, postJson(Constants.Paths.OVERLAY, body(""""tag":"ABC"""")).status)
            for (bad in listOf(""""tag":""""", """"tag":"ABCD"""", """"tag":7""")) {
                assertEquals(bad, HttpStatusCode.BadRequest, postJson(Constants.Paths.OVERLAY, body(bad)).status)
            }
        }
    }

    @Test
    fun `overlay is 503 before the start state and 405 on get`() = api(FakeCamera(ready)) {
        assertEquals(HttpStatusCode.ServiceUnavailable, postJson(Constants.Paths.OVERLAY, """{"boxes":[]}""").status)
        assertEquals(HttpStatusCode.MethodNotAllowed, client.get(Constants.Paths.OVERLAY).status)
    }

    @Test
    fun `focus on a screen point and on a snapshot point`() {
        val camera = ready(ready)
        api(camera) {
            val screen = postJson(Constants.Paths.FOCUS, """{"screen_x":0.4,"screen_y":0.6}""")
            assertEquals(HttpStatusCode.OK, screen.status)
            assertEquals(FocusState.SCANNING, screen.status().focus?.state)
            assertEquals(FocusTarget.Screen(0.4f, 0.6f), camera.focusTarget)
            val snapshot = postJson(Constants.Paths.FOCUS, """{"snapshot_x":0,"snapshot_y":1}""")
            assertEquals(HttpStatusCode.OK, snapshot.status)
            assertEquals(FocusTarget.Snapshot(0f, 1f), camera.focusTarget)
        }
    }

    @Test
    fun `focus bad bodies are 400`() = api(ready(ready)) {
        val bodies = listOf(
            "{}",
            """{"screen_x":0.4}""",
            """{"screen_x":0.4,"screen_y":0.6,"snapshot_x":0.1,"snapshot_y":0.1}""",
            """{"screen_x":"0.4","screen_y":0.6}""",
            """{"screen_x":1.5,"screen_y":0.6}""",
            """{"snapshot_x":-0.1,"snapshot_y":0.6}""",
            """{"screen_x":0.4,"screen_y":0.6,"x":1}""",
            """{"screen_x":null,"screen_y":0.6}"""
        )
        for (body in bodies) {
            val response = postJson(Constants.Paths.FOCUS, body)
            assertEquals(body, HttpStatusCode.BadRequest, response.status)
            assertEquals(body, ErrorCode.BAD_REQUEST, response.error().error)
        }
        val outside = postJson(Constants.Paths.FOCUS, """{"screen_x":0.5,"screen_y":0.95}""")
        assertEquals(HttpStatusCode.BadRequest, outside.status)
        assertEquals("outside the preview", outside.error().message)
    }

    @Test
    fun `focus is 503 before the start state and 405 on get`() = api(FakeCamera(ready)) {
        assertEquals(
            HttpStatusCode.ServiceUnavailable,
            postJson(Constants.Paths.FOCUS, """{"screen_x":0.5,"screen_y":0.5}""").status
        )
        assertEquals(HttpStatusCode.MethodNotAllowed, client.get(Constants.Paths.FOCUS).status)
    }

    @Test
    fun `in-sensor zoom on and off keeps zoom and torch`() {
        val camera = ready(ready.copy(zoomRatio = 3f, torchEnabled = true))
        api(camera) {
            val on = postJson(Constants.Paths.CAMERA, """{"in_sensor_zoom":true}""")
            assertEquals(HttpStatusCode.OK, on.status)
            val onStatus = on.status()
            assertEquals(InSensorZoomState.ON, onStatus.inSensorZoom)
            assertEquals(3f, onStatus.zoomRatio)
            assertEquals(true, onStatus.torchEnabled)
            assertTrue(client.get(Constants.Paths.STATUS).bodyAsText().contains(""""in_sensor_zoom":"on""""))
            val off = postJson(Constants.Paths.CAMERA, """{"in_sensor_zoom":false}""").status()
            assertEquals(InSensorZoomState.OFF, off.inSensorZoom)
            assertEquals(3f, off.zoomRatio)
            assertEquals(2, camera.rebinds)
        }
    }

    @Test
    fun `in-sensor zoom without vendor keys is 200 unsupported`() {
        val camera = ready(ready).apply { vendorSupported = false }
        api(camera) {
            val response = postJson(Constants.Paths.CAMERA, """{"in_sensor_zoom":true}""")
            assertEquals(HttpStatusCode.OK, response.status)
            assertTrue(response.bodyAsText().contains(""""in_sensor_zoom":"unsupported""""))
        }
    }

    @Test
    fun `camera bad bodies are 400`() = api(ready(ready)) {
        val bodies = listOf(
            "{}",
            """{"in_sensor_zoom":"true"}""",
            """{"in_sensor_zoom":1}""",
            """{"in_sensor_zoom":null}""",
            """{"in_sensor_zoom":true,"mode":"x"}""",
            "{"
        )
        for (body in bodies) {
            val response = postJson(Constants.Paths.CAMERA, body)
            assertEquals(body, HttpStatusCode.BadRequest, response.status)
            assertEquals(body, ErrorCode.BAD_REQUEST, response.error().error)
        }
    }

    @Test
    fun `camera is 503 before the start state and 405 on get`() = api(FakeCamera(ready)) {
        assertEquals(
            HttpStatusCode.ServiceUnavailable,
            postJson(Constants.Paths.CAMERA, """{"in_sensor_zoom":true}""").status
        )
        assertEquals(HttpStatusCode.MethodNotAllowed, client.get(Constants.Paths.CAMERA).status)
    }

    @Test
    fun `preview flip sets both fields and keeps the snapshot`() {
        val camera = ready(ready)
        api(camera) {
            val flipped = postJson(
                Constants.Paths.PREVIEW,
                """{"flip_horizontal":true,"flip_vertical":false}"""
            ).status()
            assertEquals(true, flipped.previewFlipHorizontal)
            assertEquals(false, flipped.previewFlipVertical)
            val status = client.get(Constants.Paths.STATUS).status()
            assertEquals(true, status.previewFlipHorizontal)
            assertEquals(0, status.rotationDegrees)
            assertArrayEquals(camera.jpeg, client.get(Constants.Paths.SNAPSHOT).readRawBytes())
            val both = postJson(Constants.Paths.PREVIEW, """{"flip_vertical":true,"flip_horizontal":true}""").status()
            assertEquals(true, both.previewFlipHorizontal)
            assertEquals(true, both.previewFlipVertical)
            val none = postJson(Constants.Paths.PREVIEW, """{"flip_horizontal":false,"flip_vertical":false}""").status()
            assertEquals(false, none.previewFlipHorizontal)
            assertEquals(false, none.previewFlipVertical)
        }
    }

    @Test
    fun `preview flip bad bodies are 400`() = api(ready(ready)) {
        val bodies = listOf(
            "{}",
            """{"flip_horizontal":true}""",
            """{"flip_vertical":true}""",
            """{"flip_horizontal":"true","flip_vertical":false}""",
            """{"flip_horizontal":1,"flip_vertical":false}""",
            """{"flip_horizontal":null,"flip_vertical":false}""",
            """{"flip_horizontal":true,"flip_vertical":false,"mirror":true}""",
            "[]",
            "{"
        )
        for (body in bodies) {
            val response = postJson(Constants.Paths.PREVIEW, body)
            assertEquals(body, HttpStatusCode.BadRequest, response.status)
            assertEquals(body, ErrorCode.BAD_REQUEST, response.error().error)
        }
        assertEquals(false, client.get(Constants.Paths.STATUS).status().previewFlipHorizontal)
    }

    @Test
    fun `preview flip is 503 before the start state and 405 on get`() = api(FakeCamera(ready)) {
        val body = """{"flip_horizontal":true,"flip_vertical":false}"""
        assertEquals(HttpStatusCode.ServiceUnavailable, postJson(Constants.Paths.PREVIEW, body).status)
        assertEquals(HttpStatusCode.MethodNotAllowed, client.get(Constants.Paths.PREVIEW).status)
    }

    @Test
    fun `rotation is 503 before the start state and 405 on get`() = api(FakeCamera(ready)) {
        assertEquals(HttpStatusCode.ServiceUnavailable, postJson(Constants.Paths.ROTATION, """{"degrees":90}""").status)
        assertEquals(HttpStatusCode.MethodNotAllowed, client.get(Constants.Paths.ROTATION).status)
    }

    @Test
    fun `unknown path is 404 not_found`() = api(ready(ready)) {
        val response = client.get("/v1/nope")
        assertEquals(HttpStatusCode.NotFound, response.status)
        assertEquals(ErrorCode.NOT_FOUND, response.error().error)
    }

    private fun freePort(): Int = ServerSocket(0).use { it.localPort }

    /** A plain HTTP call; it closes the connection first, so the server port has no TIME_WAIT for the next bind. */
    private fun call(port: Int, path: String, body: String? = null): Pair<Int, String> {
        val connection = URI("http://${Constants.Server.HOST}:$port$path").toURL().openConnection() as HttpURLConnection
        try {
            if (body != null) {
                connection.requestMethod = "POST"
                connection.doOutput = true
                connection.setRequestProperty("Content-Type", "application/json")
                connection.outputStream.use { it.write(body.toByteArray()) }
            }
            val code = connection.responseCode
            val stream = if (code <
                HttpURLConnection.HTTP_BAD_REQUEST
            ) {
                connection.inputStream
            } else {
                connection.errorStream
            }
            return code to stream.use { it.readBytes().decodeToString() }
        } finally {
            connection.disconnect()
        }
    }

    /** Waits until the server on [port] answers `/v1/health` with [version]. */
    private fun awaitHealth(port: Int, version: String) {
        val deadline = System.nanoTime() + TimeUnit.MILLISECONDS.toNanos(SERVER_WAIT_MILLIS)
        while (System.nanoTime() < deadline) {
            val answer = runCatching { call(port, Constants.Paths.HEALTH) }.getOrNull()
            if (answer?.first == HttpURLConnection.HTTP_OK && version in answer.second) return
            Thread.sleep(POLL_MILLIS)
        }
        throw AssertionError("no server with version $version on port $port")
    }

    @Test
    fun `a cancelled call is not an unexpected error`() {
        val camera = ready(ready).apply { statusError = CancellationException("server stop") }
        api(camera) { runCatching { client.get(Constants.Paths.STATUS) } }
        assertTrue(unexpected.toString(), unexpected.isEmpty())
    }

    @Test
    fun `stop does not block a thread that a running request needs (N21)`() {
        val main = Executors.newSingleThreadExecutor()
        val lifecycle = Executors.newSingleThreadExecutor()
        val client = Executors.newSingleThreadExecutor()
        try {
            val mainDispatcher = main.asCoroutineDispatcher()
            val entered = CountDownLatch(1)
            val camera = ready(ready).apply {
                // Like the rotation layout wait: the request needs the main thread again before it can end.
                beforeRotationRead = {
                    withContext(mainDispatcher) { entered.countDown() }
                    delay(REQUEST_PAUSE_MILLIS)
                    withContext(mainDispatcher) {}
                }
            }
            val port = freePort()
            val server = ApiServer(camera, APP_VERSION, port, ServerHost(lifecycle)) { unexpected += it }
            server.start()
            awaitHealth(port, APP_VERSION)
            val rotation = client.submit<Int> { call(port, Constants.Paths.ROTATION, """{"degrees":90}""").first }
            assertTrue(entered.await(SERVER_WAIT_MILLIS, TimeUnit.MILLISECONDS))
            // onDestroy calls stop on the main thread.
            val stopMillis = main.submit<Long> { measureTimeMillis { server.stop() } }.get()
            assertTrue("stop took $stopMillis ms", stopMillis < Constants.Server.STOP_GRACE_PERIOD_MILLIS)
            // The request ends inside the grace period of the stop, with its normal answer.
            assertEquals(HttpURLConnection.HTTP_OK, rotation.get(SERVER_WAIT_MILLIS, TimeUnit.MILLISECONDS))
            lifecycle.submit {}.get(SERVER_WAIT_MILLIS, TimeUnit.MILLISECONDS)
            assertTrue(unexpected.toString(), unexpected.isEmpty())
        } finally {
            listOf(main, lifecycle, client).forEach { it.shutdownNow() }
        }
    }

    @Test
    fun `a new server starts after the old one stopped, on the same port (N21)`() {
        val lifecycle = Executors.newSingleThreadExecutor()
        try {
            val host = ServerHost(lifecycle)
            val port = freePort()
            val old = ApiServer(ready(ready), OLD_APP_VERSION, port, host) { unexpected += it }
            old.start()
            awaitHealth(port, OLD_APP_VERSION)
            val next = ApiServer(ready(ready), APP_VERSION, port, host) { unexpected += it }
            // The next activity instance starts its server at once; the old stop is still queued before it.
            old.stop()
            next.start()
            awaitHealth(port, APP_VERSION)
            next.stop()
            lifecycle.submit {}.get(SERVER_WAIT_MILLIS, TimeUnit.MILLISECONDS)
            assertTrue(unexpected.toString(), unexpected.isEmpty())
        } finally {
            lifecycle.shutdownNow()
        }
    }

    @Test
    fun `a new instance that starts before the old one is destroyed takes the port (N21)`() {
        val lifecycle = Executors.newSingleThreadExecutor()
        try {
            val host = ServerHost(lifecycle)
            val port = freePort()
            val old = ApiServer(ready(ready), OLD_APP_VERSION, port, host) { unexpected += it }
            old.start()
            awaitHealth(port, OLD_APP_VERSION)
            // Android can create the next activity before it destroys the old one.
            val next = ApiServer(ready(ready), APP_VERSION, port, host) { unexpected += it }
            next.start()
            awaitHealth(port, APP_VERSION)
            // The late destroy of the old activity does not stop the next server.
            old.stop()
            lifecycle.submit {}.get(SERVER_WAIT_MILLIS, TimeUnit.MILLISECONDS)
            assertEquals(HttpURLConnection.HTTP_OK, call(port, Constants.Paths.HEALTH).first)
            next.stop()
            lifecycle.submit {}.get(SERVER_WAIT_MILLIS, TimeUnit.MILLISECONDS)
            assertTrue(unexpected.toString(), unexpected.isEmpty())
        } finally {
            lifecycle.shutdownNow()
        }
    }

    private companion object {
        const val APP_VERSION = "0.1.0"
        const val OLD_APP_VERSION = "0.0.9"
        const val SERVER_WAIT_MILLIS = 10_000L
        const val POLL_MILLIS = 20L
        const val REQUEST_PAUSE_MILLIS = 50L
        const val CONCURRENT_REQUESTS = 10
        const val OUTSIDE_PREVIEW_Y = 0.9f
        const val SENSOR_ROTATION = android.view.Surface.ROTATION_0
    }
}
