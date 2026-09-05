package com.aurora.wechatrelay.probe

internal data class OriginalImageNodeSnapshot(
    val viewId: String?,
    val className: String?,
    val label: String?,
    val visible: Boolean,
    val enabled: Boolean,
    val bounds: CaptureRect,
)

internal object WechatImageViewerPolicy {
    const val ViewerViewId = "com.tencent.mm:id/ghs"
    const val ViewerClass = "android.view.ViewGroup"
    const val ViewerDescription = "图像"
    const val ViewOriginalLabel = "查看原图"

    fun selectViewer(nodes: List<OriginalImageNodeSnapshot>, rootBounds: CaptureRect): OriginalImageNodeSnapshot? =
        nodes.singleOrNull {
            it.visible && it.viewId == ViewerViewId && it.className == ViewerClass &&
                it.label == ViewerDescription && it.bounds == rootBounds && rootBounds.width > 0 && rootBounds.height > 0
        }

    fun viewOriginalControls(
        nodes: List<OriginalImageNodeSnapshot>,
        rootBounds: CaptureRect,
    ): List<OriginalImageNodeSnapshot> = nodes.filter {
        it.visible && it.enabled && it.label?.matches(ViewOriginalLabelPattern) == true &&
            it.bounds.width > 0 && it.bounds.height > 0 &&
            rootBounds.contains(it.bounds)
    }

    fun withinDeadline(nowMillis: Long, deadlineMillis: Long): Boolean =
        deadlineMillis > 0L && nowMillis < deadlineMillis

    fun hasSettled(nowMillis: Long, observedSinceMillis: Long, settleMillis: Long): Boolean =
        observedSinceMillis > 0L && settleMillis >= 0L && nowMillis - observedSinceMillis >= settleMillis

    fun mapAspectFitCrop(
        expectedImageBounds: CaptureRect,
        viewerBounds: CaptureRect,
        rootBounds: CaptureRect,
        bitmapWidth: Int,
        bitmapHeight: Int,
    ): CaptureRect? {
        if (!rootBounds.contains(viewerBounds) || expectedImageBounds.width <= 0 || expectedImageBounds.height <= 0 ||
            viewerBounds.width <= 0 || viewerBounds.height <= 0 || bitmapWidth <= 0 || bitmapHeight <= 0
        ) return null

        val expectedRatio = expectedImageBounds.width.toDouble() / expectedImageBounds.height
        val viewerRatio = viewerBounds.width.toDouble() / viewerBounds.height
        val fitted = if (expectedRatio >= viewerRatio) {
            val height = (viewerBounds.width / expectedRatio).toInt().coerceAtLeast(1)
            val top = viewerBounds.top + (viewerBounds.height - height) / 2
            CaptureRect(viewerBounds.left, top, viewerBounds.right, top + height)
        } else {
            val width = (viewerBounds.height * expectedRatio).toInt().coerceAtLeast(1)
            val left = viewerBounds.left + (viewerBounds.width - width) / 2
            CaptureRect(left, viewerBounds.top, left + width, viewerBounds.bottom)
        }
        val verticalPadding = maxOf(2, (fitted.height + 99) / 100)
        val padded = CaptureRect(fitted.left, fitted.top - verticalPadding, fitted.right, fitted.bottom + verticalPadding)
        val minimumVerticalInset = maxOf(MinimumVerticalInsetPx, viewerBounds.height * MinimumVerticalInsetPercent / 100)
        if (!viewerBounds.contains(padded) ||
            padded.top - viewerBounds.top < minimumVerticalInset || viewerBounds.bottom - padded.bottom < minimumVerticalInset
        ) {
            return null
        }

        val scaleX = bitmapWidth.toDouble() / rootBounds.width
        val scaleY = bitmapHeight.toDouble() / rootBounds.height
        return CaptureRect(
            kotlin.math.floor((padded.left - rootBounds.left) * scaleX).toInt(),
            kotlin.math.floor((padded.top - rootBounds.top) * scaleY).toInt(),
            kotlin.math.ceil((padded.right - rootBounds.left) * scaleX).toInt(),
            kotlin.math.ceil((padded.bottom - rootBounds.top) * scaleY).toInt(),
        ).takeIf {
            it.left >= 0 && it.top >= 0 && it.right <= bitmapWidth && it.bottom <= bitmapHeight &&
                it.width > 0 && it.height > 0
        }
    }

    private val ViewOriginalLabelPattern = Regex("^${ViewOriginalLabel}(?:\\s+\\d+(?:\\.\\d+)?\\s*[KMGT]B?)?$", RegexOption.IGNORE_CASE)
    // ponytail: the observed toolbar starts at 92% height; use exact content bounds if viewer layout changes.
    private const val MinimumVerticalInsetPercent = 9
    private const val MinimumVerticalInsetPx = 64
}
