package dev.jayson.debugdevices.camera

import android.content.Context
import android.hardware.camera2.CameraManager
import android.hardware.camera2.CaptureRequest
import android.util.Log
import androidx.annotation.OptIn
import androidx.camera.camera2.interop.Camera2CameraInfo
import androidx.camera.camera2.interop.Camera2Interop
import androidx.camera.camera2.interop.ExperimentalCamera2Interop
import androidx.camera.core.Camera
import androidx.camera.core.CameraControl
import androidx.camera.core.CameraSelector
import androidx.camera.core.CameraState
import androidx.camera.core.ExtendableBuilder
import androidx.camera.core.ImageCapture
import androidx.camera.core.ImageCaptureException
import androidx.camera.core.Preview
import androidx.camera.core.TorchState
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import androidx.concurrent.futures.await
import androidx.core.content.ContextCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleOwner
import androidx.lifecycle.Observer
import androidx.lifecycle.asFlow
import java.io.ByteArrayOutputStream
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException
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
class CameraController(private val context: Context, onRotationChanged: (Int) -> Unit) : CameraPort {
    private val gate = ControlGate()

    /** Touch it on the main thread only. Declared before [imageCapture], which reads it when it is built. */
    private val rotation = RotationState { next ->
        imageCapture.targetRotation = next
        onRotationChanged(next)
    }

    /** Built again on each bind, because the in-sensor zoom parameter is set on the use case builders. */
    private var imageCapture = buildImageCapture(vendorKey = null)

    /** The rotation that the overlay shows: the snapshot rotation. */
    val effectiveRotation: Int
        get() = rotation.effectiveRotation
    private val captureLock = Mutex()
    private var camera: Camera? = null
    private var owner: LifecycleOwner? = null

    /** The in-sensor zoom state of the last bind. Main thread only. */
    var inSensorZoomState = InSensorZoomState.OFF
        private set

    /**
     * Binds the back camera. With [inSensorZoom], it sets the vendor session parameter when the camera publishes
     * the key, checks that the session streams, and binds again without it when the session fails.
     */
    suspend fun bind(owner: LifecycleOwner, previewView: PreviewView, inSensorZoom: Boolean): Camera =
        withContext(Dispatchers.Main) {
            val provider = ProcessCameraProvider.getInstance(context).await()
            val vendorKey = findVendorKey(provider)
            var state = InSensorZoomLogic.plan(inSensorZoom, vendorKey.presence)
            var bound =
                bindUseCases(
                    provider,
                    owner,
                    previewView,
                    vendorKey.key.takeIf {
                        InSensorZoomLogic.setsVendorParameter(state)
                    }
                )
            if (state == InSensorZoomState.ON) {
                val stable = InSensorZoomLogic.isStable(watchSession(owner, bound, previewView))
                state = InSensorZoomLogic.afterSessionCheck(state, stable)
                if (state == InSensorZoomState.FALLBACK) {
                    Log.w(Constants.Log.TAG, Constants.Messages.IN_SENSOR_ZOOM_FALLBACK)
                    bound = bindUseCases(provider, owner, previewView, vendorKey = null)
                }
            }
            inSensorZoomState = state
            Log.i(
                Constants.Log.TAG,
                "In-sensor zoom: requested=$inSensorZoom, sessionKey=${vendorKey.presence.inSessionKeys}, " +
                    "requestKey=${vendorKey.presence.inRequestKeys}, state=$state"
            )
            bound
        }

    private fun bindUseCases(
        provider: ProcessCameraProvider,
        owner: LifecycleOwner,
        previewView: PreviewView,
        vendorKey: CaptureRequest.Key<IntArray>?
    ): Camera {
        val previewBuilder = Preview.Builder()
        vendorKey?.let { setVendorParameter(previewBuilder, it) }
        val preview = previewBuilder.build().also { it.setSurfaceProvider(previewView.surfaceProvider) }
        imageCapture = buildImageCapture(vendorKey)
        provider.unbindAll()
        return provider.bindToLifecycle(owner, CameraSelector.DEFAULT_BACK_CAMERA, preview, imageCapture).also {
            this.owner = owner
            camera = it
        }
    }

    private fun buildImageCapture(vendorKey: CaptureRequest.Key<IntArray>?): ImageCapture {
        val builder = ImageCapture.Builder()
            .setCaptureMode(ImageCapture.CAPTURE_MODE_MAXIMIZE_QUALITY)
            .setFlashMode(ImageCapture.FLASH_MODE_OFF)
        vendorKey?.let { setVendorParameter(builder, it) }
        return builder.build().also { it.targetRotation = rotation.effectiveRotation }
    }

    /** CameraX passes a request option that is in the session keys to the session parameters too. */
    @OptIn(ExperimentalCamera2Interop::class)
    private fun <T> setVendorParameter(builder: ExtendableBuilder<T>, key: CaptureRequest.Key<IntArray>) {
        Camera2Interop.Extender(builder).setCaptureRequestOption(key, intArrayOf(Constants.InSensorZoom.ENABLED_VALUE))
    }

    private class VendorKey(val presence: VendorKeyPresence, val key: CaptureRequest.Key<IntArray>?)

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
        @Suppress("UNCHECKED_CAST")
        val key = (sessionKeys + requestKeys).firstOrNull { it.name == Constants.InSensorZoom.VENDOR_KEY }
            as CaptureRequest.Key<IntArray>?
        VendorKey(presence, key)
    } catch (cause: Exception) {
        Log.w(Constants.Log.TAG, cause)
        VendorKey(VendorKeyPresence(inSessionKeys = false, inRequestKeys = false), key = null)
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

    override suspend fun updateZoom(target: (CameraStatus) -> Float): CameraStatus = gate.control {
        withContext(Dispatchers.Main) {
            val camera = activeCamera()
            val ratio = target(readStatus(camera))
            runControl { camera.cameraControl.setZoomRatio(ratio).await() }
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
            rotationLocked = rotation.isLocked
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
