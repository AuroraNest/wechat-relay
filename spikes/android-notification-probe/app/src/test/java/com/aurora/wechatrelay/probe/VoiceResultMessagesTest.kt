package com.aurora.wechatrelay.probe

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.assertNull
import org.junit.Test

class VoiceResultMessagesTest {
    @Test fun clipboardRecoveryKeepsOriginalConversationAndRejectsTruncation() {
        val preview = VoiceResultMessages.clipboardPreview("原会话", "复制的语音文字🙂")!!
        assertEquals("原会话", preview.sender)
        assertEquals("复制的语音文字🙂", preview.body)
        assertNull(VoiceResultMessages.clipboardPreview("原会话", "字".repeat(600)))
        assertNull(VoiceResultMessages.clipboardPreview("", "语音文字"))
        assertNull(VoiceResultMessages.clipboardPreview("原会话", " "))
    }

    @Test fun longTranscriptSurvivesTheExistingPreviewLimitWithoutCuttingUnicode() {
        val transcript = "语音测试🙂 and punctuation: \" ".repeat(80).trim()
        val previews = VoiceResultMessages.previews("测试联系人", 1_000L, transcript)
        assertTrue(previews.size > 1)
        assertEquals(transcript, previews.joinToString("") { it.body.substringAfter('\n') })
        assertTrue(previews.all { it.sender == "测试联系人" && SyncProtocol.previewJson(it).toByteArray(Charsets.UTF_8).size <= 600 })
    }

    @Test fun failureRemainsExplicitInsteadOfAnEmptySuccessfulTranscript() {
        val previews = VoiceResultMessages.previews("测试联系人", 1_000L, null)
        assertEquals(1, previews.size)
        assertTrue(previews.single().body.startsWith("[语音转文字失败 "))
        assertTrue(previews.single().body.contains("未能取得可靠的转写结果"))
    }

    @Test fun knownWechatFailureExplainsWhereToFindTheOriginalVoice() {
        val previews = VoiceResultMessages.previews(
            "测试联系人",
            1_000L,
            null,
            VoiceResultMessages.VOICE_TRANSCRIPTION_FAILED,
        )
        assertTrue(previews.single().body.contains("微信未能转写这条语音, 请在微信查看原语音"))
    }

    @Test fun successfulTranscriptIgnoresFailureStage() {
        val previews = VoiceResultMessages.previews(
            "测试联系人",
            1_000L,
            "已成功转写",
            VoiceResultMessages.VOICE_TRANSCRIPTION_FAILED,
        )
        assertEquals("已成功转写", previews.single().body.substringAfter('\n'))
    }
}
