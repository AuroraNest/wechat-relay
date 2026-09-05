package com.aurora.wechatrelay.probe

import kotlin.math.ceil
import kotlin.math.floor

internal data class CaptureRect(val left: Int, val top: Int, val right: Int, val bottom: Int) {
    val width: Int get() = right - left
    val height: Int get() = bottom - top
    val centerX: Int get() = left + width / 2

    fun contains(other: CaptureRect): Boolean =
        left <= other.left && top <= other.top && right >= other.right && bottom >= other.bottom

    fun intersects(other: CaptureRect): Boolean =
        left < other.right && right > other.left && top < other.bottom && bottom > other.top
}

internal data class ImageCaptureNodeSnapshot(
    val viewId: String?,
    val className: String?,
    val contentDescription: String?,
    val visible: Boolean,
    val clickable: Boolean,
    val bounds: CaptureRect,
    val expectedAncestry: Boolean = true,
)

internal data class ImageCaptureRowSnapshot(
    val viewId: String?,
    val className: String?,
    val visible: Boolean,
    val bounds: CaptureRect,
    val containsText: Boolean,
    val images: List<ImageCaptureNodeSnapshot>,
    val avatars: List<ImageCaptureNodeSnapshot>,
)

internal data class ImageCaptureSelection(val rowBounds: CaptureRect, val imageBounds: CaptureRect)

internal object ImageCapturePolicy {
    const val RowViewId = "com.tencent.mm:id/bkj"
    const val ImageViewId = "com.tencent.mm:id/bkg"
    const val AvatarViewId = "com.tencent.mm:id/bk1"
    const val ImageDescription = "图片"
    const val RowClass = "android.widget.LinearLayout"
    const val ImageClass = "android.widget.FrameLayout"
    const val MinimumImageHeightPx = 64

    fun selectLatest(
        rows: List<ImageCaptureRowSnapshot>,
        sender: String,
        rootBounds: CaptureRect,
        chatTop: Int,
        chatBottom: Int,
    ): ImageCaptureSelection? {
        if (sender.isEmpty() || rootBounds.width <= 0 || rootBounds.height <= 0 || chatBottom <= chatTop) return null
        val chatBounds = CaptureRect(rootBounds.left, chatTop, rootBounds.right, chatBottom)
        val visibleRows = rows.filter {
            it.visible && it.viewId == RowViewId && it.className == RowClass &&
                it.bounds.width > 0 && it.bounds.height > 0 && it.bounds.intersects(chatBounds)
        }
        val latest = visibleRows.maxWithOrNull(compareBy<ImageCaptureRowSnapshot>({ it.bounds.bottom }, { it.bounds.top }))
            ?: return null
        if (visibleRows.count { it.bounds == latest.bounds } != 1) return null
        if (!rootBounds.contains(latest.bounds) || !chatBounds.contains(latest.bounds) || latest.containsText) return null

        val images = latest.images.filter {
            it.visible && it.clickable && it.expectedAncestry &&
                it.viewId == ImageViewId && it.className == ImageClass && it.contentDescription == ImageDescription &&
                it.bounds.width > 0 && it.bounds.height >= MinimumImageHeightPx &&
                latest.bounds.contains(it.bounds) && rootBounds.contains(it.bounds) && chatBounds.contains(it.bounds)
        }
        val image = images.singleOrNull() ?: return null
        val avatars = latest.avatars.filter {
            it.visible && it.viewId == AvatarViewId && it.contentDescription == "${sender}头像" &&
                it.bounds.width > 0 && it.bounds.height > 0 && latest.bounds.contains(it.bounds)
        }
        val avatar = avatars.singleOrNull() ?: return null
        val rowCenter = latest.bounds.centerX
        if (avatar.bounds.centerX >= rowCenter || image.bounds.left < avatar.bounds.right || image.bounds.centerX >= rowCenter) return null
        return ImageCaptureSelection(latest.bounds, image.bounds)
    }

    fun mapCrop(imageBounds: CaptureRect, rootBounds: CaptureRect, bitmapWidth: Int, bitmapHeight: Int): CaptureRect? {
        if (!rootBounds.contains(imageBounds) || rootBounds.width <= 0 || rootBounds.height <= 0 || bitmapWidth <= 0 || bitmapHeight <= 0) return null
        val scaleX = bitmapWidth.toDouble() / rootBounds.width
        val scaleY = bitmapHeight.toDouble() / rootBounds.height
        val mapped = CaptureRect(
            floor((imageBounds.left - rootBounds.left) * scaleX).toInt(),
            floor((imageBounds.top - rootBounds.top) * scaleY).toInt(),
            ceil((imageBounds.right - rootBounds.left) * scaleX).toInt(),
            ceil((imageBounds.bottom - rootBounds.top) * scaleY).toInt(),
        )
        return mapped.takeIf {
            it.left >= 0 && it.top >= 0 && it.right <= bitmapWidth && it.bottom <= bitmapHeight &&
                it.width > 0 && it.height >= MinimumImageHeightPx
        }
    }
}
