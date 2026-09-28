package dev.jayson.debugdevices.camera

import android.util.Log
import io.ktor.http.ContentType
import io.ktor.http.HttpStatusCode
import io.ktor.serialization.kotlinx.json.json
import io.ktor.server.application.Application
import io.ktor.server.application.ApplicationCall
import io.ktor.server.application.install
import io.ktor.server.cio.CIO
import io.ktor.server.engine.EmbeddedServer
import io.ktor.server.engine.connector
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
import java.net.InetSocketAddress
import java.net.StandardSocketOptions
import java.nio.channels.ServerSocketChannel
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledExecutorService
import java.util.concurrent.TimeUnit
import kotlin.coroutines.cancellation.CancellationException
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.isActive

/** A server that [ServerHost] starts and stops. */
interface HostedServer {
    fun startEngine()

    fun stopEngine()
}

/**
 * The one running server of the process (N21). A new activity instance can start before the old one is destroyed,
 * so a start first stops the server that still runs, and a stop acts only on its own server. Both run on one
 * background thread, in call order: the main thread never waits for running requests (a request can need the main
 * thread to end).
 *
 * A failed start or stop does not end the app: [log] gets it, and a failed start runs again every [retryMillis]
 * until it works, or until a stop or a newer start comes (for example while another app holds the port).
 */
class ServerHost(
    private val lifecycle: ScheduledExecutorService = Executors.newSingleThreadScheduledExecutor(::lifecycleThread),
    private val retryMillis: Long = Constants.Server.START_RETRY_MILLIS,
    private val log: (String, Throwable?) -> Unit = ::logServerEvent
) {
    /** The server of the newest activity instance, and the one that runs. Only the [lifecycle] thread uses them. */
    private var wanted: HostedServer? = null
    private var running: HostedServer? = null
    private var failedStarts = 0

    fun start(server: HostedServer) = lifecycle.execute {
        running?.let(::stopEngine)
        wanted = server
        failedStarts = 0
        tryStart(server)
    }

    fun stop(server: HostedServer) = lifecycle.execute {
        // No more start tries for this server.
        if (wanted === server) wanted = null
        if (running === server) stopEngine(server)
    }

    private fun tryStart(server: HostedServer) {
        if (wanted !== server) return
        try {
            server.startEngine()
        } catch (cause: Exception) {
            if (failedStarts++ == 0) log(Constants.Messages.SERVER_START_FAILED + retryMillis, cause)
            lifecycle.schedule({ tryStart(server) }, retryMillis, TimeUnit.MILLISECONDS)
            return
        }
        running = server
        if (failedStarts > 0) log(Constants.Messages.SERVER_STARTED_AFTER_RETRY + failedStarts, null)
    }

    private fun stopEngine(server: HostedServer) {
        running = null
        try {
            server.stopEngine()
        } catch (cause: Exception) {
            log(Constants.Messages.SERVER_STOP_FAILED, cause)
        }
    }

    companion object {
        /** The host of the app process: activity instances come and go, the port stays one. */
        val process = ServerHost()
    }
}

private fun lifecycleThread(task: Runnable) = Thread(task, Constants.Server.LIFECYCLE_THREAD).apply { isDaemon = true }

private fun logServerEvent(message: String, cause: Throwable?) {
    Log.w(Constants.Log.TAG, message, cause)
}

/** Runs the contract API on 127.0.0.1:8765 with the CIO engine. [start] and [stop] go through [serverHost]. */
class ApiServer(
    camera: CameraPort,
    appVersion: String,
    private val port: Int = Constants.Server.PORT,
    private val serverHost: ServerHost = ServerHost.process,
    onUnexpected: (Throwable) -> Unit
) : HostedServer {
    private val newEngine = {
        embeddedServer(
            CIO,
            configure = {
                connector {
                    host = Constants.Server.HOST
                    port = this@ApiServer.port
                }
                // A new server binds while connections of the old one can still close (TIME_WAIT).
                reuseAddress = true
            }
        ) { cameraApi(camera, appVersion, onUnexpected) }
    }

    /** The running engine. Only the [serverHost] thread uses it. */
    private var engine: EmbeddedServer<*, *>? = null

    fun start() = serverHost.start(this)

    fun stop() = serverHost.stop(this)

    /**
     * A plain bind checks the port first: a failed bind inside Ktor also goes to the coroutine exception handler,
     * which ends the app. A new engine for each start, because a failed start leaves its engine unusable.
     */
    override fun startEngine() {
        ServerSocketChannel.open().use { check ->
            check.setOption(StandardSocketOptions.SO_REUSEADDR, true)
            check.bind(InetSocketAddress(Constants.Server.HOST, port))
        }
        val next = newEngine()
        try {
            next.start(wait = false)
        } catch (cause: Exception) {
            next.stop(0L, 0L)
            throw cause
        }
        engine = next
    }

    override fun stopEngine() {
        engine?.stop(Constants.Server.STOP_GRACE_PERIOD_MILLIS, Constants.Server.STOP_TIMEOUT_MILLIS)
        engine = null
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
            // N42: a server stop or a client cancel ends the call: no answer, and not an unexpected error. A cancel
            // while the call is still active (for example a cancelled CameraX future) is an unexpected error.
            if (cause is CancellationException && !currentCoroutineContext().isActive) throw cause
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
