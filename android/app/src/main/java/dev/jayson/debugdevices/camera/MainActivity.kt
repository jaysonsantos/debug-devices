package dev.jayson.debugdevices.camera

import android.Manifest
import android.content.pm.PackageManager
import android.os.Bundle
import android.util.Log
import android.view.WindowManager
import android.widget.TextView
import androidx.activity.ComponentActivity
import androidx.activity.result.contract.ActivityResultContracts
import androidx.camera.core.Camera
import androidx.camera.core.TorchState
import androidx.camera.view.PreviewView
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
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
    private lateinit var server: ApiServer
    private lateinit var previewView: PreviewView
    private lateinit var statusView: TextView
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
        statusView = findViewById(R.id.status)
        renderStatus()

        camera = CameraController(applicationContext)
        server = ApiServer(camera, BuildConfig.VERSION_NAME) { cause ->
            Log.e(Constants.Log.TAG, Constants.Messages.UNEXPECTED, cause)
        }
        lifecycleScope.launch(Dispatchers.IO) { server.start() }

        if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED) {
            bindCamera()
        } else {
            permissionRequest.launch(Manifest.permission.CAMERA)
        }
    }

    override fun onDestroy() {
        // Stop in place, so the port is free before a new instance starts.
        server.stop()
        super.onDestroy()
    }

    private fun bindCamera() {
        lifecycleScope.launch {
            // No error from bind or the start state may escape this coroutine: it runs on the main thread,
            // so an escaped error kills the app. A coroutine cancel (activity destroyed) goes through.
            try {
                val boundCamera = camera.bind(this@MainActivity, previewView)
                observe(boundCamera)
                camera.applyStartState(boundCamera)
            } catch (cause: CancellationException) {
                throw cause
            } catch (cause: Exception) {
                Log.e(Constants.Log.TAG, Constants.Messages.START_STATE_FAILED, cause)
            }
        }
    }

    private fun observe(boundCamera: Camera) {
        boundCamera.cameraInfo.zoomState.observe(this) { state ->
            zoomRatio = state.zoomRatio
            renderStatus()
        }
        boundCamera.cameraInfo.torchState.observe(this) { state ->
            torchEnabled = state == TorchState.ON
            renderStatus()
        }
    }

    private fun renderStatus() {
        val torch = getString(if (torchEnabled) R.string.torch_on else R.string.torch_off)
        statusView.text = getString(R.string.status_label, zoomRatio, torch, Constants.Server.HOST, Constants.Server.PORT)
    }
}
