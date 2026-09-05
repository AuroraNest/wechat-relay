package com.aurora.wechatrelay.probe

import android.app.Notification
import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Canvas
import android.graphics.drawable.Icon
import android.net.Uri
import android.os.Bundle
import android.service.notification.StatusBarNotification
import java.io.ByteArrayOutputStream

data class MediaCandidate(val kind: String, val mimeType: String, val width: Int, val height: Int, val bytes: ByteArray)

/** Only serializes media that Android's public notification APIs already expose. */
object NotificationMedia {
    private const val AvatarMaxPx = 96
    private const val ImageMaxPx = 640
    private const val AvatarMaxBytes = 24 * 1024
    private const val ImageMaxBytes = 192 * 1024
    private const val ViewerImageMaxBytes = 8 * 1024 * 1024
    private const val InputMaxBytes = 2 * 1024 * 1024

    fun capture(context: Context, sbn: StatusBarNotification): List<MediaCandidate> = runCatching {
        val notification = sbn.notification
        val extras = notification.extras ?: Bundle.EMPTY
        val latest = Notification.MessagingStyle.Message.getMessagesFromBundleArray(
            extras.getParcelableArray(Notification.EXTRA_MESSAGES),
        ).lastOrNull()
        val avatar = (iconBitmap(context, latest?.senderPerson?.icon, AvatarMaxPx) ?: notification.largeIcon)
            ?.let { bitmapCandidate("avatar", it, AvatarMaxPx, AvatarMaxBytes) }
        val media = latest?.dataUri
            ?.let { uri -> uriCandidate(context, uri, latest.dataMimeType) }
            ?: extras.getParcelable<Bitmap>(Notification.EXTRA_PICTURE)
                ?.let { bitmapCandidate("image", it, ImageMaxPx, ImageMaxBytes) }
            ?: extras.getParcelable<Icon>(Notification.EXTRA_PICTURE_ICON)
                ?.let { iconBitmap(context, it, ImageMaxPx)?.let { bitmap -> bitmapCandidate("image", bitmap, ImageMaxPx, ImageMaxBytes) } }
        val kind = if (NotificationSnapshot.preview(sbn)?.body?.contains("表情") == true) "sticker" else "image"
        listOfNotNull(avatar, media?.copy(kind = kind))
    }.getOrDefault(emptyList())

    fun capturedViewerImage(bitmap: Bitmap): MediaCandidate? =
        bitmapCandidate("image", bitmap, maxOf(bitmap.width, bitmap.height), ViewerImageMaxBytes, initialQuality = 94)

    private fun uriCandidate(context: Context, uri: Uri, mimeType: String?): MediaCandidate? {
        if (mimeType?.startsWith("image/") != true || (uri.scheme != "data" && uri.scheme != "content")) return null
        val bytes = when (uri.scheme) {
            "data" -> dataBytes(uri)
            "content" -> context.contentResolver.openInputStream(uri)?.use(::boundedRead)
            else -> null
        } ?: return null
        val bitmap = decodeThumbnail(bytes) ?: return null
        return bitmapCandidate("image", bitmap, ImageMaxPx, ImageMaxBytes)
    }

    private fun dataBytes(uri: Uri): ByteArray? = runCatching {
        val content = uri.schemeSpecificPart
        require(content.substringBefore(',').contains(";base64"))
        android.util.Base64.decode(content.substringAfter(','), android.util.Base64.DEFAULT).also { require(it.size <= InputMaxBytes) }
    }.getOrNull()

    private fun boundedRead(input: java.io.InputStream): ByteArray = ByteArrayOutputStream().use { out ->
        val buffer = ByteArray(8 * 1024)
        while (true) {
            val count = input.read(buffer)
            if (count < 0) break
            if (out.size() + count > InputMaxBytes) throw IllegalArgumentException("NOTIFICATION_MEDIA_TOO_LARGE")
            out.write(buffer, 0, count)
        }
        out.toByteArray()
    }

    private fun decodeThumbnail(bytes: ByteArray): Bitmap? {
        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        BitmapFactory.decodeByteArray(bytes, 0, bytes.size, bounds)
        if (bounds.outWidth < 1 || bounds.outHeight < 1) return null
        var sample = 1
        while (maxOf(bounds.outWidth, bounds.outHeight) / sample > ImageMaxPx * 2) sample *= 2
        return BitmapFactory.decodeByteArray(bytes, 0, bytes.size, BitmapFactory.Options().apply { inSampleSize = sample })
    }

    private fun iconBitmap(context: Context, icon: Icon?, max: Int): Bitmap? = icon?.loadDrawable(context)?.let { drawable ->
        val width = maxOf(1, minOf(max, drawable.intrinsicWidth.takeIf { it > 0 } ?: max))
        val height = maxOf(1, minOf(max, drawable.intrinsicHeight.takeIf { it > 0 } ?: max))
        Bitmap.createBitmap(width, height, Bitmap.Config.ARGB_8888).also { bitmap ->
            drawable.setBounds(0, 0, width, height)
            drawable.draw(Canvas(bitmap))
        }
    }

    private fun bitmapCandidate(
        kind: String,
        bitmap: Bitmap,
        max: Int,
        maxBytes: Int,
        initialQuality: Int = 88,
    ): MediaCandidate? {
        val scale = minOf(1f, max.toFloat() / maxOf(bitmap.width, bitmap.height))
        val encoded = encode(bitmap, max, maxBytes, initialQuality)
        return encoded.takeIf { it.size <= maxBytes }?.let {
            MediaCandidate(kind, "image/jpeg", maxOf(1, (bitmap.width * scale).toInt()), maxOf(1, (bitmap.height * scale).toInt()), it)
        }
    }

    private fun encode(bitmap: Bitmap, max: Int, maxBytes: Int, initialQuality: Int): ByteArray {
        val scale = minOf(1f, max.toFloat() / maxOf(bitmap.width, bitmap.height))
        val resized = if (scale < 1f) Bitmap.createScaledBitmap(bitmap, maxOf(1, (bitmap.width * scale).toInt()), maxOf(1, (bitmap.height * scale).toInt()), true) else bitmap
        return try {
            ByteArrayOutputStream().use { out ->
                var quality = initialQuality
                do {
                    out.reset()
                    resized.compress(Bitmap.CompressFormat.JPEG, quality, out)
                    quality -= 10
                } while (out.size() > maxBytes && quality >= 20)
                out.toByteArray()
            }
        } finally {
            if (resized !== bitmap) resized.recycle()
        }
    }
}
