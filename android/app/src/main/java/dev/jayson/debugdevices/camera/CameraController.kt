package dev.jayson.debugdevices.camera

import android.annotation.SuppressLint
import android.content.Context
import android.hardware.camera2.CameraCaptureSession
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraDevice
import android.hardware.camera2.CameraManager
import android.hardware.camera2.CaptureRequest
import android.hardware.camera2.CaptureResult
import android.hardware.camera2.TotalCaptureResult
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.util.DisplayMetrics
import android.util.Log
import android.view.Surface
import android.view.View
import android.view.WindowManager
import androidx.annotation.OptIn
import androidx.camera.camera2.impl.Camera2ImplConfig
import androidx.camera.camera2.interop.Camera2CameraInfo
import androidx.camera.camera2.interop.Camera2Interop
import androidx.camera.camera2.interop.ExperimentalCamera2Interop
import androidx.camera.camera2.interop.camera2Interop
import androidx.camera.core.Camera
import androidx.camera.core.CameraControl
import androidx.camera.core.CameraSelector
import androidx.camera.core.CameraState
import androidx.camera.core.ExtendableBuilder
import androidx.camera.core.FocusMeteringAction
import androidx.camera.core.ImageCapture
import androidx.camera.core.ImageCaptureException
import androidx.camera.core.MeteringPoint
import androidx.camera.core.Preview
import androidx.camera.core.SessionConfig
import androidx.camera.core.SurfaceOrientedMeteringPointFactory
import androidx.camera.core.TorchState
import androidx.camera.core.impl.MutableOptionsBundle
import androidx.camera.core.impl.UseCaseConfig
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import androidx.concurrent.futures.await
import androidx.core.content.ContextCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleOwner
import androidx.lifecycle.Observer
import androidx.lifecycle.asFlow
import java.io.ByteArrayOutputStream
import java.util.concurrent.TimeUnit
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext

/**
 * CameraX back camera behind [CameraPort]. Every CameraX call runs on the main thread.
 * Zoom, torch, and the start state go through one [ControlGate].
 */
class CameraController(
    private val context: Context,
    onRotationChanged: (Int) -> Unit,
    private val onPreviewFlipChanged: (PreviewFlip) -> Unit,
    private val onCameraBound: (Camera) -> Unit,
    /** Gets a screen tap point in display pixels, for the focus ring. */
    private val onFocusTap: (PixelPoint) -> Unit,
    private val onOverlayChanged: () -> Unit
) : CameraPort {
    private val gate = ControlGate()

    /** Touch it on the main thread only. Declared before [imageCapture], which reads it when it is built. */
    private val rotation = RotationState { next ->
        imageCapture.targetRotation = next
        onRotationChanged(next)
    }

    /** The one place of the preview flip state. Main thread only. */
    var previewFlip = PreviewFlip.NONE
        private set

    /** Built again on each bind, because the in-sensor zoom parameter is set on the use case builders. */
    private var imageCapture = buildImageCapture(vendorParams = emptyList())

    /** The rotation that the overlay shows: the snapshot rotation. */
    val effectiveRotation: Int
        get() = rotation.effectiveRotation
    private val captureLock = Mutex()
    private var camera: Camera? = null

    /** Latest preview result values. Written on the camera thread, read on the main thread. */
    @Volatile
    private var focusSample: FocusSample? = null

    /** Static data of the bound camera. Main thread only. Null before the first bind. */
    private var focusStatic: FocusStatic? = null
    private var lensOptics: Optics? = null

    /** One instance for all sessions. It allocates a new sample only when a value changes, and never logs. */
    private val focusCallback = object : CameraCaptureSession.CaptureCallback() {
        override fun onCaptureCompleted(
            session: CameraCaptureSession,
            request: CaptureRequest,
            result: TotalCaptureResult
        ) {
            val distance = result.get(CaptureResult.LENS_FOCUS_DISTANCE)
            val afState = result.get(CaptureResult.CONTROL_AF_STATE)
            val current = focusSample
            if (current == null || current.distanceDiopters != distance || current.afState != afState) {
                focusSample = FocusSample(distance, afState)
            }
        }
    }

    /** The focus for the phone label. Main thread only. */
    fun focusInfo(): FocusInfo? = FocusLogic.info(focusStatic, focusSample)
    private var owner: LifecycleOwner? = null
    private var previewView: PreviewView? = null

    /** The one place of the overlay boxes, and the zoom when they came. Main thread only. */
    private var overlayBoxes: List<OverlayBox> = emptyList()
    private var overlayZoomAtCall = Constants.Zoom.UNIT_RATIO
    private val overlayHandler = Handler(Looper.getMainLooper())
    private val clearOverlay = Runnable {
        overlayBoxes = emptyList()
        onOverlayChanged()
    }

    /** When the last tap focus started (elapsed realtime), or null. Main thread only. */
    private var focusHoldStartedAt: Long? = null

    /** The in-sensor zoom state of the last bind. Main thread only. */
    var inSensorZoomState = InSensorZoomState.OFF
        private set

    /** The one place of the in-sensor zoom request. Off after an app start. Main thread only. */
    var inSensorZoomRequested = false

    /**
     * Binds the back camera. With [inSensorZoom], it sets the vendor session parameter when the camera publishes
     * the key, checks that the session streams, and binds again without it when the session fails.
     */
    suspend fun bind(owner: LifecycleOwner, previewView: PreviewView, restore: CameraRestore? = null): Camera =
        withContext(Dispatchers.Main) {
            this@CameraController.previewView = previewView
            val inSensorZoom = inSensorZoomRequested
            val provider = ProcessCameraProvider.getInstance(context).await()
            val vendorKey = findVendorKey(provider)
            val sessionTypeSupported = Build.VERSION.SDK_INT >= Build.VERSION_CODES.P
            var state = InSensorZoomLogic.plan(inSensorZoom, vendorKey.presence, sessionTypeSupported)
            var bound = try {
                bindUseCases(
                    provider,
                    owner,
                    previewView,
                    vendorKey.params(InSensorZoomLogic.vendorParameters(state)),
                    InSensorZoomLogic.sessionType(state)
                )
            } catch (cause: IllegalArgumentException) {
                state = InSensorZoomLogic.afterBindFailure(state)
                Log.w(Constants.Log.TAG, Constants.Messages.IN_SENSOR_ZOOM_BIND_FAILED, cause)
                bindUseCases(provider, owner, previewView, vendorParams = emptyList(), sessionType = null)
            } catch (cause: IllegalStateException) {
                state = InSensorZoomLogic.afterBindFailure(state)
                Log.w(Constants.Log.TAG, Constants.Messages.IN_SENSOR_ZOOM_BIND_FAILED, cause)
                bindUseCases(provider, owner, previewView, vendorParams = emptyList(), sessionType = null)
            }
            restore?.let {
                val started = SystemClock.elapsedRealtime()
                restoreState(bound, it, state)
                Log.i(
                    Constants.Log.TAG,
                    "Camera open again, zoom and torch set: ${SystemClock.elapsedRealtime() - started} ms"
                )
            }
            if (state == InSensorZoomState.ON) {
                val stable = InSensorZoomLogic.isStable(watchSession(owner, bound, previewView))
                state = InSensorZoomLogic.afterSessionCheck(state, stable)
                if (state == InSensorZoomState.FALLBACK) {
                    Log.w(Constants.Log.TAG, Constants.Messages.IN_SENSOR_ZOOM_FALLBACK)
                    bound = bindUseCases(provider, owner, previewView, vendorParams = emptyList(), sessionType = null)
                    restore?.let { restoreState(bound, it, state) }
                }
            }
            inSensorZoomState = state
            focusHoldStartedAt = null
            onCameraBound(bound)
            Log.i(
                Constants.Log.TAG,
                "In-sensor zoom: requested=$inSensorZoom, sessionKey=${vendorKey.presence.inSessionKeys}, " +
                    "requestKey=${vendorKey.presence.inRequestKeys}, sessionType=" +
                    "${InSensorZoomLogic.sessionType(state)?.let {
                        "0x" + it.toString(HEX_RADIX)
                    } ?: "NORMAL"}, state=$state"
            )
            bound
        }

    private fun bindUseCases(
        provider: ProcessCameraProvider,
        owner: LifecycleOwner,
        previewView: PreviewView,
        vendorParams: List<VendorParam>,
        sessionType: Int?
    ): Camera {
        val previewBuilder = Preview.Builder()
        vendorParams.forEach { setVendorParameter(previewBuilder, it) }
        setFocusCallback(previewBuilder)
        if (vendorParams.isNotEmpty() && sessionType != null) setPreviewSessionType(previewBuilder, sessionType)
        focusSample = null
        val preview = previewBuilder.build().also { it.setSurfaceProvider(previewView.surfaceProvider) }
        imageCapture = buildImageCapture(vendorParams)
        provider.unbindAll()
        val selector = CameraSelector.DEFAULT_BACK_CAMERA
        val bound = if (vendorParams.isNotEmpty() && sessionType != null &&
            Build.VERSION.SDK_INT >= Build.VERSION_CODES.P
        ) {
            // The session type is a session-level option, so it needs a SessionConfig, not loose use cases.
            val sessionConfig = SessionConfig.Builder(preview, imageCapture)
                .camera2Interop {
                    setSessionType(sessionType)
                    vendorParams.forEach { setSessionParameter(it.key, intArrayOf(it.value)) }
                }
                .also { forceSessionType(it, sessionType) }
                .build()
            provider.bindToLifecycle(owner, selector, sessionConfig)
        } else {
            provider.bindToLifecycle(owner, selector, preview, imageCapture)
        }
        return bound.also {
            this.owner = owner
            camera = it
            readStaticOptics(it)
        }
    }

    /**
     * Second part of the CameraX 1.7.0-alpha03 workaround: `SupportedSurfaceCombination` gives every stream spec
     * `SESSION_TYPE_REGULAR`, so a custom type from the SessionConfig is lost. `UseCaseCameraConfig` reads
     * `Camera2ImplConfig.SESSION_TYPE_OPTION` from the session implementation options first, and the option
     * unpacker copies the implementation options of the use case's default session config. So the Preview gets
     * its own default session config (the same template as the CameraX default) with the session type.
     */
    @SuppressLint("RestrictedApi")
    private fun setPreviewSessionType(builder: Preview.Builder, sessionType: Int) {
        val options = MutableOptionsBundle.create().apply {
            insertOption(Camera2ImplConfig.SESSION_TYPE_OPTION, sessionType)
        }
        val defaultSession = androidx.camera.core.impl.SessionConfig.Builder()
            .apply {
                setTemplateType(CameraDevice.TEMPLATE_PREVIEW)
                addImplementationOptions(options)
            }
            .build()
        builder.mutableConfig.insertOption(UseCaseConfig.OPTION_DEFAULT_SESSION_CONFIG, defaultSession)
    }

    /**
     * Workaround for CameraX 1.7.0-alpha03: `SessionConfigCamera2Interop.setSessionType` writes
     * `camera2.cameraCaptureSession.sessionType`, but `SessionConfig.Builder.build()` reads
     * `camerax.core.useCase.sessionType`, and the session option unpacker does not copy the Camera2 key. This fix
     * alone did not change the mode on 7fad170e (still NORMAL); [setPreviewSessionType] did. Not tested alone
     * without this one. Library-internal API: experiment only.
     */
    @SuppressLint("RestrictedApi")
    private fun forceSessionType(builder: SessionConfig.Builder, sessionType: Int) {
        builder.interopMutableConfig.insertOption(UseCaseConfig.OPTION_SESSION_TYPE, sessionType)
    }

    @OptIn(ExperimentalCamera2Interop::class)
    private fun setFocusCallback(builder: Preview.Builder) {
        Camera2Interop.Extender(builder).setSessionCaptureCallback(focusCallback)
    }

    /** Reads the focus calibration, the minimum focus distance, the focal length, and the sensor width. */
    @OptIn(ExperimentalCamera2Interop::class)
    private fun readStaticOptics(bound: Camera) {
        val info = Camera2CameraInfo.from(bound.cameraInfo)
        focusStatic = FocusStatic(
            calibration = info.getCameraCharacteristic(CameraCharacteristics.LENS_INFO_FOCUS_DISTANCE_CALIBRATION),
            minDistanceDiopters = info.getCameraCharacteristic(CameraCharacteristics.LENS_INFO_MINIMUM_FOCUS_DISTANCE)
        )
        lensOptics = Optics(
            focalLengthMm = info.getCameraCharacteristic(CameraCharacteristics.LENS_INFO_AVAILABLE_FOCAL_LENGTHS)
                ?.firstOrNull() ?: Constants.Focus.UNKNOWN_MM,
            sensorWidthMm = info.getCameraCharacteristic(CameraCharacteristics.SENSOR_INFO_PHYSICAL_SIZE)?.width
                ?: Constants.Focus.UNKNOWN_MM,
            outputWidthPx = Constants.Focus.UNKNOWN_PX
        )
    }

    /** The optics with the snapshot width, which ImageCapture knows after the bind. */
    private fun readOptics(): Optics {
        val optics = lensOptics
            ?: throw ApiException(ErrorCode.CAMERA_NOT_READY, Constants.Messages.CAMERA_NOT_READY)
        val resolution = imageCapture.resolutionInfo?.resolution
            ?: throw ApiException(ErrorCode.CAMERA_NOT_READY, Constants.Messages.CAMERA_NOT_READY)
        return optics.copy(outputWidthPx = FocusLogic.outputWidthPx(resolution.width, resolution.height))
    }

    private fun buildImageCapture(vendorParams: List<VendorParam>): ImageCapture {
        val builder = ImageCapture.Builder()
            .setCaptureMode(ImageCapture.CAPTURE_MODE_MAXIMIZE_QUALITY)
            .setFlashMode(ImageCapture.FLASH_MODE_OFF)
        vendorParams.forEach { setVendorParameter(builder, it) }
        return builder.build().also { it.targetRotation = rotation.effectiveRotation }
    }

    /** CameraX passes a request option that is in the session keys to the session parameters too. */
    @OptIn(ExperimentalCamera2Interop::class)
    private fun <T> setVendorParameter(builder: ExtendableBuilder<T>, param: VendorParam) {
        Camera2Interop.Extender(builder).setCaptureRequestOption(param.key, intArrayOf(param.value))
    }

    /** A vendor int32 key (the framework gives it the type int[]) and its value. */
    private class VendorParam(val key: CaptureRequest.Key<IntArray>, val value: Int)

    /** The in-sensor zoom key presence, and every vendor int32 key of the camera by name. */
    private class VendorKey(
        val presence: VendorKeyPresence,
        val keysByName: Map<String, CaptureRequest.Key<IntArray>>
    ) {
        /** The parameters whose key the camera publishes. A missing key is skipped. */
        fun params(values: Map<String, Int>): List<VendorParam> =
            values.mapNotNull { (name, value) -> keysByName[name]?.let { VendorParam(it, value) } }
    }

    /** Finds the vendor key in the session and request key lists of the back camera. Never throws. */
    @OptIn(ExperimentalCamera2Interop::class)
    private fun findVendorKey(provider: ProcessCameraProvider): VendorKey = try {
        val info = CameraSelector.DEFAULT_BACK_CAMERA.filter(provider.availableCameraInfos).first()
        val cameraId = Camera2CameraInfo.from(info).cameraId
        val manager = context.getSystemService(CameraManager::class.java)
        val characteristics = manager.getCameraCharacteristics(cameraId)
        val sessionKeys = characteristics.availableSessionKeys.orEmpty()
        val requestKeys = characteristics.availableCaptureRequestKeys.orEmpty()
        val presence = InSensorZoomLogic.presence(sessionKeys.map { it.name }, requestKeys.map { it.name })

        // The framework gives vendor keys an array type: int32 becomes int[]. A plain Int fails in
        // CameraMetadataNative with "Not an array" (seen on 7fad170e), and CameraX only logs it.
        val names = InSensorZoomLogic.vendorParameters(InSensorZoomState.ON).keys

        @Suppress("UNCHECKED_CAST")
        val keys = (sessionKeys + requestKeys).filter { it.name in names }
            .associate { it.name to it as CaptureRequest.Key<IntArray> }
        VendorKey(presence, keys)
    } catch (cause: Exception) {
        Log.w(Constants.Log.TAG, cause)
        VendorKey(VendorKeyPresence(inSessionKeys = false, inRequestKeys = false), keysByName = emptyMap())
    }

    /** Collects preview and camera error signals of a new session for the check window. */
    private suspend fun watchSession(
        owner: LifecycleOwner,
        bound: Camera,
        previewView: PreviewView
    ): Set<SessionEvent> {
        val events = mutableSetOf<SessionEvent>()
        val streamObserver = Observer<PreviewView.StreamState> { state ->
            if (state == PreviewView.StreamState.STREAMING) events += SessionEvent.PREVIEW_STREAMING
        }
        val cameraObserver = Observer<CameraState> { state ->
            state.error?.let {
                Log.w(Constants.Log.TAG, "Camera error during the in-sensor zoom check: code=${it.code}", it.cause)
                events += SessionEvent.CAMERA_ERROR
            }
        }
        previewView.previewStreamState.observe(owner, streamObserver)
        bound.cameraInfo.cameraState.observe(owner, cameraObserver)
        try {
            delay(Constants.InSensorZoom.SESSION_CHECK_MILLIS)
        } finally {
            previewView.previewStreamState.removeObserver(streamObserver)
            bound.cameraInfo.cameraState.removeObserver(cameraObserver)
        }
        return events
    }

    /** Sets the zoom and the torch again as soon as the new session opens (before the in-sensor session check). */
    private suspend fun restoreState(bound: Camera, restore: CameraRestore, state: InSensorZoomState) {
        bound.cameraInfo.cameraState.asFlow().first { it.type == CameraState.Type.OPEN }
        // A new session starts at 1x, so the restore follows the same zoom path as a request.
        setZoomAlongPath(bound, Constants.Zoom.UNIT_RATIO, restore.zoomRatio, state)
        if (bound.cameraInfo.hasFlashUnit()) {
            runControl {
                bound.cameraControl.enableTorch(restore.torchEnabled).await()
            }
        }
    }

    /** Sets the zoom through [InSensorZoomLogic.zoomPath], with a short settle time between the steps. */
    private suspend fun setZoomAlongPath(
        target: Camera,
        from: Float,
        to: Float,
        state: InSensorZoomState = inSensorZoomState
    ) {
        val path = InSensorZoomLogic.zoomPath(state, from, to)
        path.forEachIndexed { index, step ->
            runControl { target.cameraControl.setZoomRatio(step).await() }
            if (index < path.lastIndex) delay(Constants.InSensorZoom.ENTRY_SETTLE_MILLIS)
        }
    }

    /** Binds again with the current request and keeps the zoom and the torch. The caller holds the gate lock. */
    private suspend fun reconfigure(reason: String) {
        val current = activeCamera()
        val restore = CameraRestore(
            zoomRatio = current.cameraInfo.zoomState.value?.zoomRatio ?: Constants.Zoom.UNIT_RATIO,
            torchEnabled = current.cameraInfo.torchState.value == TorchState.ON
        )
        val lifecycleOwner =
            owner ?: throw ApiException(ErrorCode.CAMERA_NOT_READY, Constants.Messages.CAMERA_NOT_READY)
        val view = previewView ?: throw ApiException(ErrorCode.CAMERA_NOT_READY, Constants.Messages.CAMERA_NOT_READY)
        val started = SystemClock.elapsedRealtime()
        bind(lifecycleOwner, view, restore)
        Log.i(
            Constants.Log.TAG,
            "Rebind ($reason) at zoom ${restore.zoomRatio}: ${SystemClock.elapsedRealtime() - started} ms, " +
                "state=$inSensorZoomState"
        )
    }

    override suspend fun setInSensorZoom(enabled: Boolean): CameraStatus = gate.control {
        withContext(Dispatchers.Main) {
            activeCamera()
            if (InSensorZoomLogic.needsReconfigure(inSensorZoomRequested, inSensorZoomState, enabled)) {
                inSensorZoomRequested = enabled
                reconfigure(Constants.Messages.REBIND_REASON_API)
            }
            readStatus(activeCamera())
        }
    }

    /**
     * Waits until the camera is open, then sets the start state of the contract: zoom at min, torch off.
     * The API returns 503 until this is done. A failure goes to the caller, and the API opens anyway.
     */
    suspend fun applyStartState(camera: Camera) = gate.start {
        withContext(Dispatchers.Main) {
            camera.cameraInfo.cameraState.asFlow().first { it.type == CameraState.Type.OPEN }
            val minZoomRatio = camera.cameraInfo.zoomState.value?.minZoomRatio
                ?: throw ApiException(ErrorCode.CAMERA_NOT_READY, Constants.Messages.CAMERA_NOT_READY)
            runControl { camera.cameraControl.setZoomRatio(ZoomLogic.startRatio(minZoomRatio)).await() }
            if (camera.cameraInfo.hasFlashUnit()) {
                runControl { camera.cameraControl.enableTorch(Constants.Start.TORCH_ENABLED).await() }
            }
        }
    }

    /** An `OrientationEventListener` angle. Call it on the main thread. */
    fun onSensorAngle(angle: Int) = rotation.onSensorAngle(angle)

    override suspend fun status(): CameraStatus = withContext(Dispatchers.Main) {
        gate.checkReady()
        readStatus(activeCamera())
    }

    override suspend fun setOverlay(boxes: List<OverlayBox>): CameraStatus = gate.control {
        withContext(Dispatchers.Main) {
            val camera = activeCamera()
            overlayBoxes = boxes
            overlayZoomAtCall = camera.cameraInfo.zoomState.value?.zoomRatio ?: Constants.Zoom.UNIT_RATIO
            overlayHandler.removeCallbacks(clearOverlay)
            if (boxes.isNotEmpty()) overlayHandler.postDelayed(clearOverlay, Constants.Overlay.TTL_MILLIS)
            onOverlayChanged()
            readStatus(camera)
        }
    }

    /** The overlay boxes in the pixels of a view with the preview's bounds. Main thread only. */
    fun overlayRects(viewWidth: Float, viewHeight: Float): List<Pair<PixelRect, String>> {
        val camera = camera ?: return emptyList()
        val view = previewView ?: return emptyList()
        if (overlayBoxes.isEmpty() || viewWidth <= 0f || viewHeight <= 0f) return emptyList()
        val resolution = imageCapture.resolutionInfo?.resolution ?: return emptyList()
        val previewRotation = camera.cameraInfo.getSensorRotationDegrees(Surface.ROTATION_0)
        val sideways = previewRotation % (2 * Constants.Orientation.BUCKET_DEGREES) != 0
        val geometry = OverlayGeometry(
            zoomAtCall = overlayZoomAtCall,
            zoomNow = camera.cameraInfo.zoomState.value?.zoomRatio ?: overlayZoomAtCall,
            snapshotRotation = camera.cameraInfo.getSensorRotationDegrees(imageCapture.targetRotation),
            previewRotation = previewRotation,
            imageWidth = (if (sideways) resolution.height else resolution.width).toFloat(),
            imageHeight = (if (sideways) resolution.width else resolution.height).toFloat(),
            viewWidth = viewWidth,
            viewHeight = viewHeight,
            fill = view.scaleType.name.startsWith(FILL_SCALE_PREFIX),
            mirroredX = view.scaleX < 0f,
            mirroredY = view.scaleY < 0f
        )
        return overlayBoxes.mapNotNull { box -> OverlayLogic.boxToView(box, geometry)?.let { it to box.label } }
    }

    override suspend fun focusAt(target: FocusTarget): CameraStatus = gate.control {
        withContext(Dispatchers.Main) {
            val camera = activeCamera()
            val point = when (target) {
                is FocusTarget.Screen -> screenMeteringPoint(target)
                is FocusTarget.Snapshot -> snapshotMeteringPoint(camera, target)
            }
            val action = FocusMeteringAction.Builder(point, FocusMeteringAction.FLAG_AF or FocusMeteringAction.FLAG_AE)
                .setAutoCancelDuration(Constants.FocusTap.HOLD_SECONDS, TimeUnit.SECONDS)
                .build()
            // A new action replaces the running one. The response comes at once; focus.state shows the progress.
            camera.cameraControl.startFocusAndMetering(action)
            focusHoldStartedAt = SystemClock.elapsedRealtime()
            readStatus(camera)
        }
    }

    private fun screenMeteringPoint(target: FocusTarget.Screen): MeteringPoint {
        val view = previewView ?: throw ApiException(ErrorCode.CAMERA_NOT_READY, Constants.Messages.CAMERA_NOT_READY)
        val geometry = previewGeometry(view)
        val local = FocusTapLogic.screenToPreview(target.x, target.y, geometry)
            ?: throw ApiException(ErrorCode.BAD_REQUEST, Constants.Messages.FOCUS_OUTSIDE_PREVIEW)
        onFocusTap(PixelPoint(target.x * geometry.displayWidth, target.y * geometry.displayHeight))
        return view.meteringPointFactory.createPoint(local.x, local.y)
    }

    private fun snapshotMeteringPoint(camera: Camera, target: FocusTarget.Snapshot): MeteringPoint {
        val rotation = camera.cameraInfo.getSensorRotationDegrees(imageCapture.targetRotation)
        val (x, y) = FocusTapLogic.snapshotToSurface(target.x, target.y, rotation)
        return SurfaceOrientedMeteringPointFactory(NORMALIZED_SIZE, NORMALIZED_SIZE, imageCapture).createPoint(x, y)
    }

    /**
     * The preview position on the display. The parent is never mirrored, so its screen location plus the layout
     * offset is exact; `getLocationOnScreen` of a mirrored view would give the mirrored corner.
     */
    private fun previewGeometry(view: PreviewView): PreviewGeometry {
        val parentLocation = IntArray(2)
        (view.parent as View).getLocationOnScreen(parentLocation)
        val display = displaySize()
        return PreviewGeometry(
            displayWidth = display.x,
            displayHeight = display.y,
            viewLeft = parentLocation[0] + view.left,
            viewTop = parentLocation[1] + view.top,
            viewWidth = view.width,
            viewHeight = view.height,
            mirroredX = view.scaleX < 0f,
            mirroredY = view.scaleY < 0f
        )
    }

    /** The full display in its natural portrait orientation, like the screen stream. */
    private fun displaySize(): android.graphics.Point {
        val windowManager = context.getSystemService(WindowManager::class.java)
        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            val bounds = windowManager.maximumWindowMetrics.bounds
            android.graphics.Point(bounds.width(), bounds.height())
        } else {
            val metrics = DisplayMetrics()
            @Suppress("DEPRECATION")
            windowManager.defaultDisplay.getRealMetrics(metrics)
            android.graphics.Point(metrics.widthPixels, metrics.heightPixels)
        }
    }

    /** A zoom change ends a tap focus hold early. */
    private fun endFocusHold(camera: Camera) {
        val startedAt = focusHoldStartedAt ?: return
        if (SystemClock.elapsedRealtime() - startedAt < Constants.FocusTap.HOLD_MILLIS) {
            camera.cameraControl.cancelFocusAndMetering()
        }
        focusHoldStartedAt = null
    }

    override suspend fun updateZoom(target: (CameraStatus) -> Float): CameraStatus = gate.control {
        withContext(Dispatchers.Main) {
            val camera = activeCamera()
            endFocusHold(camera)
            val ratio = target(readStatus(camera))
            setZoomAlongPath(camera, camera.cameraInfo.zoomState.value?.zoomRatio ?: Constants.Zoom.UNIT_RATIO, ratio)
            // The zoom LiveData updates later, so report the ratio that CameraX accepted.
            readStatus(camera).copy(zoomRatio = ratio)
        }
    }

    override suspend fun setTorch(enabled: Boolean): CameraStatus = gate.control {
        withContext(Dispatchers.Main) {
            val camera = activeCamera()
            runControl { camera.cameraControl.enableTorch(enabled).await() }
            readStatus(camera).copy(torchEnabled = enabled)
        }
    }

    override suspend fun setPreviewFlip(flip: PreviewFlip): CameraStatus = gate.control {
        withContext(Dispatchers.Main) {
            val camera = activeCamera()
            if (flip != previewFlip) {
                previewFlip = flip
                onPreviewFlipChanged(flip)
            }
            readStatus(camera)
        }
    }

    override suspend fun setRotation(lockedRotation: Int?): CameraStatus = gate.control {
        withContext(Dispatchers.Main) {
            val camera = activeCamera()
            rotation.lock(lockedRotation)
            readStatus(camera)
        }
    }

    override suspend fun capture(): ByteArray = captureLock.withLock {
        withContext(Dispatchers.Main) {
            gate.checkReady()
            activeCamera()
            val output = ByteArrayOutputStream()
            val options = ImageCapture.OutputFileOptions.Builder(output).build()
            suspendCancellableCoroutine { continuation ->
                imageCapture.takePicture(
                    options,
                    ContextCompat.getMainExecutor(context),
                    object : ImageCapture.OnImageSavedCallback {
                        override fun onImageSaved(outputFileResults: ImageCapture.OutputFileResults) {
                            continuation.resume(output.toByteArray())
                        }

                        override fun onError(exception: ImageCaptureException) {
                            continuation.resumeWithException(
                                ApiException(
                                    ErrorCode.CAPTURE_FAILED,
                                    exception.message ?: Constants.Messages.CAPTURE_FAILED,
                                    exception
                                )
                            )
                        }
                    }
                )
            }
        }
    }

    private fun activeCamera(): Camera {
        val camera = camera ?: throw ApiException(ErrorCode.CAMERA_NOT_READY, Constants.Messages.CAMERA_NOT_READY)
        // The contract: 503 while the app is not in the foreground.
        val foreground = owner?.lifecycle?.currentState?.isAtLeast(Lifecycle.State.RESUMED) == true
        if (!foreground) throw ApiException(ErrorCode.CAMERA_NOT_READY, Constants.Messages.CAMERA_NOT_ACTIVE)
        return camera
    }

    private fun readStatus(camera: Camera): CameraStatus {
        val info = camera.cameraInfo
        val zoom = info.zoomState.value
            ?: throw ApiException(ErrorCode.CAMERA_NOT_READY, Constants.Messages.CAMERA_NOT_READY)
        return CameraStatus(
            zoomRatio = zoom.zoomRatio,
            minZoomRatio = zoom.minZoomRatio,
            maxZoomRatio = zoom.maxZoomRatio,
            torchEnabled = info.torchState.value == TorchState.ON,
            hasFlashUnit = info.hasFlashUnit(),
            rotationDegrees = rotation.effectiveDegrees,
            rotationLocked = rotation.isLocked,
            previewFlipHorizontal = previewFlip.horizontal,
            previewFlipVertical = previewFlip.vertical,
            focus = focusInfo(),
            optics = readOptics(),
            inSensorZoom = inSensorZoomState,
            overlayBoxes = overlayBoxes.size
        )
    }

    /** CameraX cancels a call when the camera closes. With [ControlGate], only a closed camera cancels. */
    private suspend fun runControl(block: suspend () -> Unit) {
        try {
            block()
        } catch (cause: CameraControl.OperationCanceledException) {
            throw ApiException(ErrorCode.CAMERA_NOT_READY, cause.message ?: Constants.Messages.CAMERA_NOT_ACTIVE, cause)
        }
    }
}

private const val HEX_RADIX = 16

/** Width and height for a metering point factory that takes normalized coordinates. */
private const val NORMALIZED_SIZE = 1f

/** `PreviewView.ScaleType` names that crop to fill the view (FILL_START, FILL_CENTER, FILL_END). */
private const val FILL_SCALE_PREFIX = "FILL"
