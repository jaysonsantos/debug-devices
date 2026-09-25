package dev.jayson.debugdevices.camera

/** What the HTTP API needs from the camera. Every call throws [ApiException] on failure. */
interface CameraPort {
    suspend fun status(): CameraStatus

    /**
     * Sets the zoom to [target] of the current status. The read and the change run under one lock, so parallel
     * `step` requests do not lose updates.
     */
    suspend fun updateZoom(target: (CameraStatus) -> Float): CameraStatus

    suspend fun setTorch(enabled: Boolean): CameraStatus

    /** Shows highlight boxes over the preview (not in snapshots). An empty list clears them. */
    suspend fun setOverlay(boxes: List<OverlayBox>, arrows: List<OverlayArrow>): CameraStatus

    /** Focuses and meters on one point. The response comes at once; the focus state shows the progress. */
    suspend fun focusAt(target: FocusTarget): CameraStatus

    /** Turns the vendor in-sensor zoom on or off. It binds again and keeps the zoom and the torch. */
    suspend fun setInSensorZoom(enabled: Boolean): CameraStatus

    /** Sets the given camera settings (null = keep). In-sensor zoom first, then the autofocus mode. */
    suspend fun setCameraSettings(inSensorZoom: Boolean?, afMode: AfMode?): CameraStatus

    /** Mirrors the on-screen preview only. Snapshots do not change. */
    suspend fun setPreviewFlip(flip: PreviewFlip): CameraStatus

    /** Locks the snapshot rotation to a `Surface.ROTATION_*` value, or goes back to auto with null. */
    suspend fun setRotation(lockedRotation: Int?): CameraStatus

    /** One full still capture as JPEG bytes. */
    suspend fun capture(): ByteArray
}
