package com.aurora.wechatrelay.probe

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class VoiceClipboardPolicyTest {
    @Test fun rejectsStaleEmptyAndMultiItemClipboards() {
        val now = 1_000_000L
        assertNull(VoiceClipboardPolicy.acceptedText(1, true, now - 10_001L, now - 20_000L, now, "转写"))
        assertNull(VoiceClipboardPolicy.acceptedText(1, true, now, now - 1L, now, "  "))
        assertNull(VoiceClipboardPolicy.acceptedText(2, true, now, now - 1L, now, "转写"))
        assertEquals("转写", VoiceClipboardPolicy.acceptedText(1, true, now, now - 1L, now, "转写"))
    }
}
