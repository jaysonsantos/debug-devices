package dev.jayson.debugdevices.camera

/** Pure zoom rules from the contract. No Android types, so JVM unit tests cover it. */
object ZoomLogic {
    fun clamp(ratio: Float, min: Float, max: Float): Float = ratio.coerceIn(min, max)

    fun step(current: Float, step: ZoomStep, min: Float, max: Float): Float {
        val next = when (step) {
            ZoomStep.IN -> current * Constants.Zoom.STEP_FACTOR
            ZoomStep.OUT -> current / Constants.Zoom.STEP_FACTOR
        }
        return clamp(next, min, max)
    }

    /** Returns the clamped target ratio, or throws [ApiException] with [ErrorCode.BAD_REQUEST]. */
    fun resolve(request: ZoomRequest, status: CameraStatus): Float {
        val ratio = request.ratio
        val step = request.step
        return when {
            ratio != null && step == null -> {
                if (!ratio.isFinite()) {
                    throw ApiException(ErrorCode.BAD_REQUEST, Constants.Messages.ZOOM_RATIO_NOT_FINITE)
                }
                clamp(ratio, status.minZoomRatio, status.maxZoomRatio)
            }

            step != null && ratio == null -> step(status.zoomRatio, step, status.minZoomRatio, status.maxZoomRatio)

            else -> throw ApiException(ErrorCode.BAD_REQUEST, Constants.Messages.ZOOM_NEEDS_ONE_FIELD)
        }
    }
}
