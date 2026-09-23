package dev.jayson.debugdevices.camera

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
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Test

class ApiServerTest {
    private class FakeCamera(var status: CameraStatus?) : CameraPort {
        val jpeg = byteArrayOf(0xFF.toByte(), 0xD8.toByte(), 0xFF.toByte(), 0xD9.toByte())
        var captureError: ApiException? = null

        override suspend fun status(): CameraStatus =
            status ?: throw ApiException(ErrorCode.CAMERA_NOT_READY, Constants.Messages.CAMERA_NOT_READY)

        override suspend fun setZoomRatio(ratio: Float): CameraStatus =
            status().copy(zoomRatio = ratio).also { status = it }

        override suspend fun setTorch(enabled: Boolean): CameraStatus =
            status().copy(torchEnabled = enabled).also { status = it }

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
    )

    private fun api(camera: FakeCamera, block: suspend ApplicationTestBuilder.() -> Unit) = testApplication {
        application { cameraApi(camera, APP_VERSION) }
        block()
    }

    private suspend fun ApplicationTestBuilder.postJson(path: String, body: String): HttpResponse =
        client.post(path) {
            contentType(ContentType.Application.Json)
            setBody(body)
        }

    private suspend fun HttpResponse.status(): CameraStatus = ApiJson.decodeFromString(bodyAsText())

    private suspend fun HttpResponse.error(): ApiError = ApiJson.decodeFromString(bodyAsText())

    @Test
    fun `health returns ok and the version`() = api(FakeCamera(null)) {
        val response = client.get(Constants.Paths.HEALTH)
        assertEquals(HttpStatusCode.OK, response.status)
        assertEquals("""{"ok":true,"app_version":"0.1.0"}""", response.bodyAsText())
    }

    @Test
    fun `status uses snake case`() = api(FakeCamera(ready)) {
        val body = client.get(Constants.Paths.STATUS).bodyAsText()
        assertEquals(
            """{"zoom_ratio":1.0,"min_zoom_ratio":1.0,"max_zoom_ratio":8.0,"torch_enabled":false,"has_flash_unit":true}""",
            body,
        )
    }

    @Test
    fun `status before bind is 503 camera_not_ready`() = api(FakeCamera(null)) {
        val response = client.get(Constants.Paths.STATUS)
        assertEquals(HttpStatusCode.ServiceUnavailable, response.status)
        assertEquals(ErrorCode.CAMERA_NOT_READY, response.error().error)
        assertEquals("""{"error":"camera_not_ready","message":"Camera is not bound yet"}""", response.bodyAsText())
    }

    @Test
    fun `zoom step in and out`() = api(FakeCamera(ready.copy(zoomRatio = 2f))) {
        assertEquals(3f, postJson(Constants.Paths.ZOOM, """{"step":"in"}""").status().zoomRatio)
        assertEquals(2f, postJson(Constants.Paths.ZOOM, """{"step":"out"}""").status().zoomRatio)
    }

    @Test
    fun `zoom ratio is clamped`() = api(FakeCamera(ready)) {
        assertEquals(8f, postJson(Constants.Paths.ZOOM, """{"ratio":20}""").status().zoomRatio)
        assertEquals(1f, postJson(Constants.Paths.ZOOM, """{"ratio":0.2}""").status().zoomRatio)
        assertEquals(2.5f, postJson(Constants.Paths.ZOOM, """{"ratio":2.5}""").status().zoomRatio)
    }

    @Test
    fun `zoom with both fields is 400`() = api(FakeCamera(ready)) {
        val response = postJson(Constants.Paths.ZOOM, """{"ratio":2,"step":"in"}""")
        assertEquals(HttpStatusCode.BadRequest, response.status)
        assertEquals(ErrorCode.BAD_REQUEST, response.error().error)
    }

    @Test
    fun `zoom with a bad step is 400`() = api(FakeCamera(ready)) {
        val response = postJson(Constants.Paths.ZOOM, """{"step":"sideways"}""")
        assertEquals(HttpStatusCode.BadRequest, response.status)
        assertEquals(ErrorCode.BAD_REQUEST, response.error().error)
    }

    @Test
    fun `zoom with broken json is 400`() = api(FakeCamera(ready)) {
        val response = postJson(Constants.Paths.ZOOM, "{")
        assertEquals(HttpStatusCode.BadRequest, response.status)
        assertEquals(ErrorCode.BAD_REQUEST, response.error().error)
    }

    @Test
    fun `zoom without content type is 400`() = api(FakeCamera(ready)) {
        val response = client.post(Constants.Paths.ZOOM) { setBody("""{"ratio":2}""") }
        assertEquals(HttpStatusCode.BadRequest, response.status)
        assertEquals(ErrorCode.BAD_REQUEST, response.error().error)
    }

    @Test
    fun `torch on and off`() = api(FakeCamera(ready)) {
        assertEquals(true, postJson(Constants.Paths.TORCH, """{"enabled":true}""").status().torchEnabled)
        assertEquals(false, postJson(Constants.Paths.TORCH, """{"enabled":false}""").status().torchEnabled)
    }

    @Test
    fun `torch without flash unit is 409`() = api(FakeCamera(ready.copy(hasFlashUnit = false))) {
        val response = postJson(Constants.Paths.TORCH, """{"enabled":true}""")
        assertEquals(HttpStatusCode.Conflict, response.status)
        assertEquals(ErrorCode.NO_FLASH_UNIT, response.error().error)
    }

    @Test
    fun `snapshot returns jpeg bytes`() {
        val camera = FakeCamera(ready)
        api(camera) {
            val response = client.get(Constants.Paths.SNAPSHOT)
            assertEquals(HttpStatusCode.OK, response.status)
            assertEquals(ContentType.Image.JPEG, response.contentType()?.withoutParameters())
            assertArrayEquals(camera.jpeg, response.readRawBytes())
        }
    }

    @Test
    fun `snapshot failure is 500 capture_failed`() {
        val camera = FakeCamera(ready).apply { captureError = ApiException(ErrorCode.CAPTURE_FAILED, "boom") }
        api(camera) {
            val response = client.get(Constants.Paths.SNAPSHOT)
            assertEquals(HttpStatusCode.InternalServerError, response.status)
            assertEquals(ErrorCode.CAPTURE_FAILED, response.error().error)
        }
    }

    @Test
    fun `unknown path is 404 not_found`() = api(FakeCamera(ready)) {
        val response = client.get("/v1/nope")
        assertEquals(HttpStatusCode.NotFound, response.status)
        assertEquals(ErrorCode.NOT_FOUND, response.error().error)
    }

    private companion object {
        const val APP_VERSION = "0.1.0"
    }
}
