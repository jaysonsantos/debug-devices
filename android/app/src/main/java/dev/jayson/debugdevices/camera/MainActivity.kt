package dev.jayson.debugdevices.camera

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle
import android.util.Log
import android.view.Gravity
import android.view.OrientationEventListener
import android.view.View
import android.view.WindowManager
import android.widget.FrameLayout
import android.widget.TextView
import androidx.activity.ComponentActivity
import androidx.activity.result.contract.ActivityResultContracts
import androidx.camera.core.Camera
import androidx.camera.core.CameraInfo
import androidx.camera.core.TorchState
import androidx.camera.view.PreviewView
import androidx.core.content.ContextCompat
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.lifecycleScope
import androidx.lifecycle.repeatOnLifecycle
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

/**
 * Shows the back camera and serves the HTTP API while the activity lives.
 *
 * The server follows the activity (onCreate to onDestroy), not a foreground service: Android lets an app
 * use the camera only while it is visible or while a camera-type foreground service runs, and the app keeps
 * the screen on anyway. A service adds a notification and permissions for no gain here.
 */
class MainActivity : ComponentActivity() {
    private lateinit var camera: CameraController
    private var bindJob: Job? = null

    /** The camera info whose zoom and torch the label observes. A rebind can give a new one. */
    private var observedInfo: CameraInfo? = null
    private lateinit var server: ApiServer
    private lateinit var previewView: PreviewView
    private lateinit var statusView: TextView
    private lateinit var focusRing: View
    private lateinit var overlayView: OverlayView
    private lateinit var safeArea: FrameLayout
    private lateinit var overlay: FrameLayout
    private lateinit var orientationListener: OrientationEventListener
    private var zoomRatio = 1f
    private var torchEnabled = false

    private val permissionRequest = registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        if (granted) bindCamera() else statusView.setText(R.string.permission_denied)
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        setContentView(R.layout.activity_main)
        previewView = findViewById(R.id.preview)
        // A TextureView follows scaleX and scaleY, so the preview flip works. The default SurfaceView ignores them
        // (seen on the test phone: the label said "flip H", but the preview did not change).
        previewView.implementationMode = PreviewView.ImplementationMode.COMPATIBLE
        statusView = findViewById(R.id.status)
        focusRing = findViewById(R.id.focus_ring)
        overlayView = findViewById(R.id.highlight_overlay)
        safeArea = findViewById(R.id.safe_area)
        overlay = findViewById(R.id.overlay)
        // Keep the label out of the status bar, the navigation bar, and the camera cutout.
        ViewCompat.setOnApplyWindowInsetsListener(safeArea) { view, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.displayCutout())
            view.setPadding(bars.left, bars.top, bars.right, bars.bottom)
            insets
        }
        safeArea.addOnLayoutChangeListener { _, _, _, _, _, _, _, _, _ -> layoutOverlay() }
        renderStatus()

        camera = CameraController(
            applicationContext,
            onRotationChanged = { next -> onRotationChanged(next) },
            onPreviewFlipChanged = { applyPreviewFlip() },
            onCameraBound = { bound -> observe(bound) },
            onFocusTap = { point -> showFocusRing(point) },
            onOverlayChanged = { overlayView.invalidate() }
        )
        overlayView.scene = { width, height, inset, length -> camera.overlayScene(width, height, inset, length) }
        // The activity stays in portrait, so the preview never restarts. Only the snapshot and the label follow
        // the physical orientation.
        orientationListener = object : OrientationEventListener(this) {
            override fun onOrientationChanged(orientation: Int) {
                camera.onSensorAngle(orientation)
            }
        }
        server = ApiServer(camera, BuildConfig.VERSION_NAME) { cause ->
            Log.e(Constants.Log.TAG, Constants.Messages.UNEXPECTED, cause)
        }
        lifecycleScope.launch(Dispatchers.IO) { server.start() }
        // The focus distance changes without an event, so the label reads it again while the app is visible.
        lifecycleScope.launch {
            repeatOnLifecycle(Lifecycle.State.STARTED) {
                while (true) {
                    renderStatus()
                    delay(Constants.Focus.LABEL_REFRESH_MILLIS)
                }
            }
        }

        // adb test helper: `--ez in_sensor_zoom true` at start. The API switch is POST /v1/camera.
        camera.inSensorZoomRequested = readInSensorZoom(intent, current = false)
        if (hasCameraPermission()) {
            bindCamera()
        } else {
            permissionRequest.launch(Manifest.permission.CAMERA)
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        val next = readInSensorZoom(intent, current = camera.inSensorZoomRequested)
        if (next != camera.inSensorZoomRequested && hasCameraPermission()) {
            lifecycleScope.launch {
                try {
                    camera.setInSensorZoom(next)
                } catch (cause: CancellationException) {
                    throw cause
                } catch (cause: Exception) {
                    Log.w(Constants.Log.TAG, Constants.Messages.REBIND_FAILED, cause)
                }
            }
        }
    }

    private fun readInSensorZoom(intent: Intent?, current: Boolean): Boolean {
        val extra = Constants.InSensorZoom.INTENT_EXTRA
        return InSensorZoomLogic.requested(
            current = current,
            extraPresent = intent?.hasExtra(extra) == true,
            extraValue = intent?.getBooleanExtra(extra, false) == true
        )
    }

    private fun hasCameraPermission(): Boolean =
        ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED

    override fun onResume() {
        super.onResume()
        if (orientationListener.canDetectOrientation()) orientationListener.enable()
    }

    override fun onPause() {
        orientationListener.disable()
        super.onPause()
    }

    override fun onDestroy() {
        // Stop in place, so the port is free before a new instance starts.
        server.stop()
        super.onDestroy()
    }

    private fun bindCamera() {
        bindJob?.cancel()
        bindJob = lifecycleScope.launch {
            // No error from bind or the start state may escape this coroutine: it runs on the main thread,
            // so an escaped error kills the app. A coroutine cancel (activity destroyed) goes through.
            try {
                val boundCamera = camera.bind(this@MainActivity, previewView)
                camera.applyStartState(boundCamera)
            } catch (cause: CancellationException) {
                throw cause
            } catch (cause: Exception) {
                Log.e(Constants.Log.TAG, Constants.Messages.START_STATE_FAILED, cause)
            }
        }
    }

    private fun onRotationChanged(next: Int) {
        layoutOverlay()
        applyPreviewFlip()
        overlayView.invalidate()
        Log.i(Constants.Log.TAG, Constants.Messages.ROTATION_CHANGED + OrientationLogic.surfaceDegrees(next))
    }

    /**
     * Turns the overlay so the label reads upright in the viewer's top-left corner. The overlay is centered in
     * the safe area; when sideways it takes the safe area size with width and height swapped, so after the turn
     * it covers the safe area exactly.
     */
    private fun layoutOverlay() {
        val width = safeArea.width - safeArea.paddingLeft - safeArea.paddingRight
        val height = safeArea.height - safeArea.paddingTop - safeArea.paddingBottom
        if (width <= 0 || height <= 0) return
        val rotation = camera.effectiveRotation
        val sideways = OrientationLogic.isSideways(rotation)
        val overlayWidth = if (sideways) height else width
        val overlayHeight = if (sideways) width else height
        val params = overlay.layoutParams as FrameLayout.LayoutParams
        if (params.width != overlayWidth || params.height != overlayHeight) {
            overlay.layoutParams = FrameLayout.LayoutParams(overlayWidth, overlayHeight, Gravity.CENTER)
        }
        overlay.rotation = OrientationLogic.surfaceDegrees(rotation).toFloat()
    }

    private fun observe(boundCamera: Camera) {
        if (boundCamera.cameraInfo === observedInfo) return
        observedInfo?.zoomState?.removeObservers(this)
        observedInfo?.torchState?.removeObservers(this)
        observedInfo = boundCamera.cameraInfo
        boundCamera.cameraInfo.zoomState.observe(this) { state ->
            zoomRatio = state.zoomRatio
            renderStatus()
            overlayView.invalidate()
        }
        boundCamera.cameraInfo.torchState.observe(this) { state ->
            torchEnabled = state == TorchState.ON
            renderStatus()
        }
    }

    /** Shows the focus ring centred on a display point for a short time. */
    private fun showFocusRing(point: PixelPoint) {
        val rootLocation = IntArray(2)
        (focusRing.parent as View).getLocationOnScreen(rootLocation)
        val half = resources.getDimension(R.dimen.focus_ring_size) / 2
        focusRing.translationX = point.x - rootLocation[0] - half
        focusRing.translationY = point.y - rootLocation[1] - half
        focusRing.visibility = View.VISIBLE
        focusRing.removeCallbacks(hideFocusRing)
        focusRing.postDelayed(hideFocusRing, Constants.FocusTap.RING_MILLIS)
    }

    private val hideFocusRing = Runnable { focusRing.visibility = View.GONE }

    /** Mirrors only the preview view. The overlay with the label is a sibling, so its text stays readable. */
    private fun applyPreviewFlip() {
        val scale = PreviewFlipLogic.scale(camera.previewFlip, camera.effectiveRotation)
        previewView.scaleX = scale.scaleX
        previewView.scaleY = scale.scaleY
        overlayView.invalidate()
        renderStatus()
    }

    private fun renderStatus() {
        val torch = getString(if (torchEnabled) R.string.torch_on else R.string.torch_off)
        val flip = if (::camera.isInitialized) camera.previewFlip else PreviewFlip.NONE
        val distanceCm = if (::camera.isInitialized) FocusLogic.distanceCm(camera.focusInfo()) else null
        val distanceText = distanceCm?.let {
            getString(R.string.status_separator) +
                getString(R.string.focus_distance, it)
        }
            .orEmpty()
        val flipText = listOfNotNull(
            getString(R.string.flip_horizontal).takeIf { flip.horizontal },
            getString(R.string.flip_vertical).takeIf { flip.vertical }
        ).joinToString(separator = "") { getString(R.string.status_separator) + it }
        statusView.text =
            getString(
                R.string.status_label,
                zoomRatio,
                torch,
                Constants.Server.HOST,
                Constants.Server.PORT,
                flipText,
                distanceText
            )
    }
}
