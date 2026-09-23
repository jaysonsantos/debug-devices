package dev.jayson.debugdevices.camera

/** What the HTTP API needs from the camera. Every call throws [ApiException] on failure. */
interface CameraPort {
    suspend fun status(): CameraStatus

    suspend fun setZoomRatio(ratio: Float): CameraStatus

    suspend fun setTorch(enabled: Boolean): CameraStatus

    /** One full still capture as JPEG bytes. */
    suspend fun capture(): ByteArray
}
