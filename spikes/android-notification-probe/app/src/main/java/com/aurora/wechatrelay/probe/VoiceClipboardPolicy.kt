package com.aurora.wechatrelay.probe

internal object VoiceClipboardPolicy {
    const val MaximumAgeMillis = 10_000L
    const val MaximumTextLength = 16_000

    fun acceptedText(
        itemCount: Int,
        hasPlainTextMimeType: Boolean,
        timestampMillis: Long,
        copiedAfterMillis: Long,
        nowMillis: Long,
        text: CharSequence?,
    ): String? {
        if (itemCount != 1 || !hasPlainTextMimeType || timestampMillis !in copiedAfterMillis..nowMillis) return null
        if (nowMillis - timestampMillis > MaximumAgeMillis || text.isNullOrBlank() || text.length > MaximumTextLength) return null
        return text.toString()
    }
}
