package dev.jayson.debugdevices.camera

import android.content.Context
import androidx.camera.core.Camera
import androidx.camera.core.CameraControl
import androidx.camera.core.CameraSelector
import androidx.camera.core.CameraState
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
import androidx.lifecycle.asFlow
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import java.io.ByteArrayOutputStream
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException

/**
 * CameraX back camera behind [CameraPort]. Every CameraX call runs on the main thread.
 * Zoom, torch, and the start state go through one [ControlGate].
 */
class CameraController(private val context: Context) : CameraPort {
    private val imageCapture = ImageCapture.Builder()
        .setCaptureMode(ImageCapture.CAPTURE_MODE_MAXIMIZE_QUALITY)
        .setFlashMode(ImageCapture.FLASH_MODE_OFF)
        .build()
    private val gate = ControlGate()
    private val captureLock = Mutex()
    private var camera: Camera? = null
    private var owner: LifecycleOwner? = null

    suspend fun bind(owner: LifecycleOwner, previewView: PreviewView): Camera = withContext(Dispatchers.Main) {
        val provider = ProcessCameraProvider.getInstance(context).await()
        val preview = Preview.Builder().build().also { it.setSurfaceProvider(previewView.surfaceProvider) }
        provider.unbindAll()
        provider.bindToLifecycle(owner, CameraSelector.DEFAULT_BACK_CAMERA, preview, imageCapture).also {
            this@CameraController.owner = owner
            camera = it
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
                                    exception,
                                ),
                            )
                        }
                    },
                )
            }
        }
    }

    private fun activeCamera(): Camera {
        val camera = camera ?: throw ApiException(ErrorCode.CAMERA_NOT_READY, Constants.Messages.CAMERA_NOT_READY)
        val started = owner?.lifecycle?.currentState?.isAtLeast(Lifecycle.State.STARTED) == true
        if (!started) throw ApiException(ErrorCode.CAMERA_NOT_READY, Constants.Messages.CAMERA_NOT_ACTIVE)
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
