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
import io.ktor.server.response.header
import io.ktor.server.response.respond
import io.ktor.server.response.respondBytes
import io.ktor.server.routing.get
import io.ktor.server.routing.post
import io.ktor.server.routing.routing
import java.util.concurrent.Executor
import java.util.concurrent.Executors
import kotlin.coroutines.cancellation.CancellationException

/**
 * The one running server of the process (N21). A new activity instance can start before the old one is destroyed,
 * so a start first stops the server that still runs, and a stop acts only on its own server. Both run on one
 * background thread, in call order: the main thread never waits for running requests (a request can need the main
 * thread to end).
 */
class ServerHost(private val lifecycle: Executor = Executors.newSingleThreadExecutor(::lifecycleThread)) {
    /** Only the [lifecycle] thread reads and writes it. */
    private var running: ApiServer? = null

    fun start(server: ApiServer) = lifecycle.execute {
        running?.stopEngine()
        server.startEngine()
        running = server
    }

    fun stop(server: ApiServer) = lifecycle.execute {
        if (running === server) {
            server.stopEngine()
            running = null
        }
    }

    companion object {
        /** The host of the app process: activity instances come and go, the port stays one. */
        val process = ServerHost()
    }
}

private fun lifecycleThread(task: Runnable) = Thread(task, Constants.Server.LIFECYCLE_THREAD).apply { isDaemon = true }

/** Runs the contract API on 127.0.0.1:8765 with the CIO engine. [start] and [stop] go through [host]. */
class ApiServer(
    camera: CameraPort,
    appVersion: String,
    port: Int = Constants.Server.PORT,
    private val host: ServerHost = ServerHost.process,
    onUnexpected: (Throwable) -> Unit
) {
    private val engine = embeddedServer(CIO, port = port, host = Constants.Server.HOST) {
        cameraApi(camera, appVersion, onUnexpected)
    }

    fun start() = host.start(this)

    fun stop() = host.stop(this)

    internal fun startEngine() {
        engine.start(wait = false)
    }

    internal fun stopEngine() {
        engine.stop(Constants.Server.STOP_GRACE_PERIOD_MILLIS, Constants.Server.STOP_TIMEOUT_MILLIS)
    }
}

/** [onUnexpected] gets every error that is not part of the contract, so the caller can log the stack trace. */
fun Application.cameraApi(camera: CameraPort, appVersion: String, onUnexpected: (Throwable) -> Unit) {
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
            // A server stop cancels the running calls (N21): no answer, and not an unexpected error.
            if (cause is CancellationException) throw cause
            onUnexpected(cause)
            call.respondError(ErrorCode.INTERNAL_ERROR, Constants.Messages.UNEXPECTED)
        }
        status(HttpStatusCode.NotFound) { call, _ ->
            call.respondError(ErrorCode.NOT_FOUND, Constants.Messages.NOT_FOUND)
        }
        status(HttpStatusCode.MethodNotAllowed) { call, _ ->
            call.respondError(ErrorCode.METHOD_NOT_ALLOWED, Constants.Messages.METHOD_NOT_ALLOWED)
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
            // Refuse a bad body with 400 before the camera state matters.
            ZoomLogic.validate(request)
            call.respond(camera.updateZoom { status -> ZoomLogic.resolve(request, status) })
        }
        post(Constants.Paths.TORCH) {
            val request = call.receive<TorchRequest>()
            if (!camera.status().hasFlashUnit) {
                throw ApiException(ErrorCode.NO_FLASH_UNIT, Constants.Messages.NO_FLASH_UNIT)
            }
            call.respond(camera.setTorch(request.enabled))
        }
        post(Constants.Paths.ROTATION) {
            val request = call.receive<RotationRequest>()
            call.respond(camera.setRotation(OrientationLogic.lockedRotationFor(request)))
        }
        post(Constants.Paths.PREVIEW) {
            val request = call.receive<PreviewRequest>()
            call.respond(camera.setPreviewFlip(PreviewFlipLogic.fromRequest(request)))
        }
        post(Constants.Paths.CAMERA) {
            val request = call.receive<CameraSettingsRequest>()
            CameraSettingsLogic.validate(request)
            call.respond(camera.setCameraSettings(request.inSensorZoom, request.afMode))
        }
        post(Constants.Paths.FOCUS) {
            val request = call.receive<FocusRequest>()
            call.respond(camera.focusAt(FocusTapLogic.target(request)))
        }
        post(Constants.Paths.OVERLAY) {
            val status = when (val command = OverlayLogic.command(call.receive<OverlayRequest>())) {
                is OverlayCommand.SetShapes -> camera.setOverlay(command.boxes, command.arrows)
                is OverlayCommand.SetVisible -> camera.setOverlayVisible(command.visible)
            }
            call.respond(status)
        }
        get(Constants.Paths.SNAPSHOT) {
            val snapshot = camera.capture()
            call.response.header(Constants.Snapshot.HEADER_ROTATION_DEGREES, snapshot.rotationDegrees.toString())
            call.response.header(Constants.Snapshot.HEADER_APP_START_ID, snapshot.appStartId)
            call.respondBytes(snapshot.jpeg, ContentType.Image.JPEG)
        }
    }
}

private suspend fun ApplicationCall.respondError(code: ErrorCode, message: String) {
    respond(code.status, ApiError(code, message))
}
