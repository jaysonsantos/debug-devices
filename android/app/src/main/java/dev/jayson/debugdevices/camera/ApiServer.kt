package dev.jayson.debugdevices.camera

import io.ktor.http.ContentType
import io.ktor.http.HttpStatusCode
import io.ktor.serialization.kotlinx.json.json
import io.ktor.server.application.Application
import io.ktor.server.application.ApplicationCall
import io.ktor.server.application.install
import io.ktor.server.cio.CIO
import io.ktor.server.engine.embeddedServer
import io.ktor.server.plugins.BadRequestException
import io.ktor.server.plugins.ContentTransformationException
import io.ktor.server.plugins.contentnegotiation.ContentNegotiation
import io.ktor.server.plugins.statuspages.StatusPages
import io.ktor.server.request.receive
import io.ktor.server.response.respond
import io.ktor.server.response.respondBytes
import io.ktor.server.routing.get
import io.ktor.server.routing.post
import io.ktor.server.routing.routing

/** Runs the contract API on 127.0.0.1:8765 with the CIO engine. */
class ApiServer(camera: CameraPort, appVersion: String) {
    private val engine = embeddedServer(CIO, port = Constants.Server.PORT, host = Constants.Server.HOST) {
        cameraApi(camera, appVersion)
    }

    fun start() {
        engine.start(wait = false)
    }

    fun stop() {
        engine.stop(Constants.Server.STOP_GRACE_PERIOD_MILLIS, Constants.Server.STOP_TIMEOUT_MILLIS)
    }
}

fun Application.cameraApi(camera: CameraPort, appVersion: String) {
    install(ContentNegotiation) { json(ApiJson) }
    install(StatusPages) {
        exception<ApiException> { call, cause -> call.respondError(cause.code, cause.message) }
        exception<BadRequestException> { call, _ ->
            call.respondError(ErrorCode.BAD_REQUEST, Constants.Messages.BAD_BODY)
        }
        exception<ContentTransformationException> { call, _ ->
            call.respondError(ErrorCode.BAD_REQUEST, Constants.Messages.BAD_BODY)
        }
        exception<Throwable> { call, cause ->
            call.respondError(ErrorCode.CAMERA_NOT_READY, cause.message ?: Constants.Messages.UNEXPECTED)
        }
        status(HttpStatusCode.NotFound) { call, _ ->
            call.respondError(ErrorCode.NOT_FOUND, Constants.Messages.NOT_FOUND)
        }
        status(HttpStatusCode.MethodNotAllowed) { call, status ->
            call.respond(status, ApiError(ErrorCode.BAD_REQUEST, Constants.Messages.METHOD_NOT_ALLOWED))
        }
        status(HttpStatusCode.UnsupportedMediaType) { call, _ ->
            call.respondError(ErrorCode.BAD_REQUEST, Constants.Messages.BAD_BODY)
        }
    }
    routing {
        get(Constants.Paths.HEALTH) { call.respond(HealthResponse(ok = true, appVersion = appVersion)) }
        get(Constants.Paths.STATUS) { call.respond(camera.status()) }
        post(Constants.Paths.ZOOM) {
            val request = call.receive<ZoomRequest>()
            val target = ZoomLogic.resolve(request, camera.status())
            call.respond(camera.setZoomRatio(target))
        }
        post(Constants.Paths.TORCH) {
            val request = call.receive<TorchRequest>()
            if (!camera.status().hasFlashUnit) {
                throw ApiException(ErrorCode.NO_FLASH_UNIT, Constants.Messages.NO_FLASH_UNIT)
            }
            call.respond(camera.setTorch(request.enabled))
        }
        get(Constants.Paths.SNAPSHOT) { call.respondBytes(camera.capture(), ContentType.Image.JPEG) }
    }
}

private suspend fun ApplicationCall.respondError(code: ErrorCode, message: String) {
    respond(code.status, ApiError(code, message))
}
