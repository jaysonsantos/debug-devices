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
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.yield
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Test

class ApiServerTest {
    /** Uses a real [ControlGate], like the CameraX controller. [gate] is ready unless a test says otherwise. */
    private class FakeCamera(var status: CameraStatus?, val gate: ControlGate = ControlGate()) : CameraPort {
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

        override suspend fun setRotation(lockedRotation: Int?): CameraStatus = gate.control {
            val degrees = OrientationLogic.surfaceDegrees(lockedRotation ?: SENSOR_ROTATION)
            status().copy(rotationDegrees = degrees, rotationLocked = lockedRotation != null).also { status = it }
        }

        override suspend fun capture(): ByteArray {
            captureError?.let { throw it }
            status()
            return jpeg
        }
    }

    private val ready = CameraStatus(
        zoomRatio = 1f,
        minZoomRatio = 1f,
        maxZoomRatio = 8f,
        torchEnabled = false,
        hasFlashUnit = true,
        rotationDegrees = 0,
        rotationLocked = false
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
            """{"zoom_ratio":1.0,"min_zoom_ratio":1.0,"max_zoom_ratio":8.0,"torch_enabled":false,"has_flash_unit":true,"rotation_degrees":0,"rotation_locked":false}""",
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

    private companion object {
        const val APP_VERSION = "0.1.0"
        const val CONCURRENT_REQUESTS = 10
        const val SENSOR_ROTATION = android.view.Surface.ROTATION_0
    }
}
