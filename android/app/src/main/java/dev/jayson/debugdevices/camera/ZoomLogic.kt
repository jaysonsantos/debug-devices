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

    /**
     * The zoom ratio after an app start: 1x (the main camera) when it is inside the range, else the minimum. A phone
     * with an ultrawide lens has a minimum below 1x, and starting there would start on the ultrawide.
     */
    fun startRatio(minZoomRatio: Float, maxZoomRatio: Float): Float =
        if (Constants.Zoom.UNIT_RATIO in minZoomRatio..maxZoomRatio) Constants.Zoom.UNIT_RATIO else minZoomRatio

    /** Throws [ApiException] with [ErrorCode.BAD_REQUEST] when the request is not exactly one finite field. */
    fun validate(request: ZoomRequest) {
        val ratio = request.ratio
        if ((ratio == null) == (request.step == null)) {
            throw ApiException(ErrorCode.BAD_REQUEST, Constants.Messages.ZOOM_NEEDS_ONE_FIELD)
        }
        if (ratio != null && !ratio.isFinite()) {
            throw ApiException(ErrorCode.BAD_REQUEST, Constants.Messages.ZOOM_RATIO_NOT_FINITE)
        }
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
