package com.aurora.wechatrelay.probe

import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

internal object VoiceResultMessages {
    const val VOICE_TRANSCRIPTION_FAILED = "VOICE_TRANSCRIPTION_FAILED"

    fun clipboardPreview(sender: String, text: String): Preview? {
        if (sender.isBlank() || text.isBlank()) return null
        return SyncProtocol.normalizePreview(sender, text, "text").takeIf {
            it.sender == sender && it.body == text
        }
    }

    fun previews(sender: String, postTime: Long, transcript: String?, failureStage: String? = null): List<Preview> {
        val time = SimpleDateFormat("HH:mm:ss", Locale.ROOT).format(Date(postTime))
        if (transcript.isNullOrBlank()) {
            val message = if (failureStage == VOICE_TRANSCRIPTION_FAILED) {
                "微信未能转写这条语音, 请在微信查看原语音."
            } else {
                "未能取得可靠的转写结果, 请在微信查看原语音."
            }
            return listOf(SyncProtocol.normalizePreview(sender, "[语音转文字失败 $time]\n$message", "text"))
        }
        val characters = transcript.trim().codePoints().toArray()
        val result = mutableListOf<Preview>()
        var offset = 0
        while (offset < characters.size) {
            val heading = "[语音转文字 $time ${result.size + 1}]\n"
            val remaining = String(characters, offset, characters.size - offset)
            val preview = SyncProtocol.normalizePreview(sender, heading + remaining, "text")
            check(preview.sender == sender && preview.body.startsWith(heading))
            val part = preview.body.removePrefix(heading)
            val consumed = part.codePointCount(0, part.length)
            check(consumed > 0)
            result.add(preview)
            offset += consumed
        }
        return result
    }
}
