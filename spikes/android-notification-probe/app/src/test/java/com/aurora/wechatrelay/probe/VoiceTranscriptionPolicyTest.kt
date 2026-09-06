package com.aurora.wechatrelay.probe

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class VoiceTranscriptionPolicyTest {
    @Test fun expandedRowStaysSelectableButOffscreenMarkersDoNot() {
        assertTrue(VoiceTranscriptionPolicy.intersectsViewport(2_200, 2_900, 300, 2_400))
        assertTrue(VoiceTranscriptionPolicy.intersectsViewport(200, 500, 300, 2_400))
        assertTrue(VoiceTranscriptionPolicy.intersectsViewport(400, 500, 300, 2_400))
        assertFalse(VoiceTranscriptionPolicy.intersectsViewport(2_400, 2_900, 300, 2_400))
        assertFalse(VoiceTranscriptionPolicy.intersectsViewport(100, 300, 300, 2_400))
        assertFalse(VoiceTranscriptionPolicy.intersectsViewport(400, 400, 300, 2_400))
        assertFalse(VoiceTranscriptionPolicy.intersectsViewport(400, 500, 2_400, 300))
    }

    @Test fun anUnlockedVoiceWorkflowReturnsHomeEvenOnFailureWithoutTakingOverAnotherApp() {
        assertTrue(VoiceTranscriptionPolicy.shouldRequestHome(false, true, true))
        assertFalse(VoiceTranscriptionPolicy.shouldRequestHome(true, true, true))
        assertFalse(VoiceTranscriptionPolicy.shouldRequestHome(false, false, true))
        assertFalse(VoiceTranscriptionPolicy.shouldRequestHome(false, true, false))
    }

    @Test fun metadataRepostDoesNotCreateAnotherVoiceButDistinctMessagesSurvive() {
        val detector = DuplicateDetector(250L)
        val voice = Preview("测试联系人", "[语音] 13\"", "text")
        fun key(source: String = "notification", user: Int = 0, preview: Preview = voice) =
            SyncProtocol.shortWindowDuplicateKey(source, user, preview)
        assertFalse(detector.isDuplicateCandidate(key(), 1_000))
        assertTrue(detector.isDuplicateCandidate(key(), 1_140))
        assertFalse(detector.isDuplicateCandidate(key(source = "other"), 1_150))
        assertFalse(detector.isDuplicateCandidate(key(user = 999), 1_150))
        assertFalse(detector.isDuplicateCandidate(key(preview = voice.copy(body = "[语音] 3\"")), 1_150))
        assertFalse(detector.isDuplicateCandidate(key(), 1_391))
    }

    @Test fun expandedTranscriptMayClipOnlyOlderRowsOfTheSamePinnedNode() {
        val signatures = listOf("甲|2\"", "甲|3\"", "甲|13\"")
        val pinned = requireNotNull(VoiceTranscriptionPolicy.pinVoiceSelection(signatures, 0))
        assertTrue(VoiceTranscriptionPolicy.matchesPinnedVoice(pinned, signatures.drop(1), 0, sameVoiceNode = true))
        assertTrue(VoiceTranscriptionPolicy.matchesPinnedVoice(pinned, signatures.takeLast(1), 0, sameVoiceNode = true))
        assertFalse(VoiceTranscriptionPolicy.matchesPinnedVoice(pinned, signatures.drop(1), 0))
        assertFalse(VoiceTranscriptionPolicy.matchesPinnedVoice(pinned, emptyList(), 0, sameVoiceNode = true))
        assertFalse(VoiceTranscriptionPolicy.matchesPinnedVoice(pinned, listOf("乙|13\""), 0, sameVoiceNode = true))
        assertFalse(VoiceTranscriptionPolicy.matchesPinnedVoice(pinned, listOf("甲|2\"", "甲|13\""), 0, sameVoiceNode = true))
        assertFalse(VoiceTranscriptionPolicy.matchesPinnedVoice(pinned, signatures + "甲|13\"", 0, sameVoiceNode = true))
        assertFalse(VoiceTranscriptionPolicy.matchesPinnedVoice(pinned, signatures.drop(1), 1, sameVoiceNode = true))
        val queued = requireNotNull(VoiceTranscriptionPolicy.pinVoiceSelection(signatures, 1))
        assertTrue(VoiceTranscriptionPolicy.matchesPinnedVoice(queued, signatures.drop(1), 1, sameVoiceNode = true))
        assertFalse(VoiceTranscriptionPolicy.matchesPinnedVoice(queued, signatures.takeLast(1), 1, sameVoiceNode = true))
        assertFalse(VoiceTranscriptionPolicy.matchesPinnedVoice(queued, listOf("甲|3\"", "甲|9\""), 1, sameVoiceNode = true))
    }

    @Test fun onlyFreshExactVoiceNotificationsInNamedChatsTriggerTranscription() {
        val voice = Preview("测试联系人", "[语音]", "text")
        assertTrue(VoiceTranscriptionPolicy.shouldTranscribe(voice, "测试联系人", false, 0))
        val timedVoice = voice.copy(body = "[语音] 3\"")
        assertTrue(VoiceTranscriptionPolicy.shouldTranscribe(timedVoice, "测试联系人", false, 0))
        assertEquals("3\"", VoiceTranscriptionPolicy.notificationDuration(timedVoice))
        assertEquals(null, VoiceTranscriptionPolicy.notificationDuration(voice))
        assertTrue(VoiceTranscriptionPolicy.shouldTranscribe(voice.copy(body = "[2条]测试联系人: [语音] 6\""), "测试联系人", false, 0))
        assertFalse(VoiceTranscriptionPolicy.shouldTranscribe(voice.copy(body = "[语音] 3\"其他内容"), "测试联系人", false, 0))
        assertTrue(VoiceTranscriptionPolicy.shouldTranscribe(voice.copy(body = "[2条]测试联系人: [语音]"), "测试联系人", false, 120_000))
        assertFalse(VoiceTranscriptionPolicy.shouldTranscribe(voice.copy(body = "请发[语音]"), "测试联系人", false, 0))
        assertFalse(VoiceTranscriptionPolicy.shouldTranscribe(voice.copy(body = "[2条]其他人: [语音]"), "测试联系人", false, 0))
        assertFalse(VoiceTranscriptionPolicy.shouldTranscribe(voice, "", false, 0))
        assertFalse(VoiceTranscriptionPolicy.shouldTranscribe(voice, "测试联系人", true, 0))
        assertFalse(VoiceTranscriptionPolicy.shouldTranscribe(voice, "测试联系人", false, -1))
        assertFalse(VoiceTranscriptionPolicy.shouldTranscribe(voice, "测试联系人", false, 120_001))
    }

    @Test fun voiceOffsetPinsTheIntendedVoiceFromTheRight() {
        assertEquals(2, VoiceTranscriptionPolicy.latestVoiceIndex(listOf(false, true, true)))
        assertEquals(1, VoiceTranscriptionPolicy.latestVoiceIndex(listOf(false, true, true), 1))
        assertEquals(null, VoiceTranscriptionPolicy.latestVoiceIndex(listOf(false, true), 2))
        assertEquals(null, VoiceTranscriptionPolicy.latestVoiceIndex(listOf(true), -1))
    }

    @Test fun pinnedVoiceAllowsLayoutChangesButRejectsListOrOffsetChanges() {
        val signatures = listOf("甲|6\"|语音6秒", "乙|8\"|语音8秒")
        val pinned = requireNotNull(VoiceTranscriptionPolicy.pinVoiceSelection(signatures, 0))
        assertTrue(VoiceTranscriptionPolicy.matchesPinnedVoice(pinned, signatures.toList(), 0))
        assertFalse(VoiceTranscriptionPolicy.matchesPinnedVoice(pinned, signatures + "甲|6\"|语音6秒", 0))
        assertFalse(VoiceTranscriptionPolicy.matchesPinnedVoice(pinned, signatures, 1))
    }

    @Test fun transcriptMustRemainUnchangedForTheSettleWindow() {
        assertFalse(VoiceTranscriptionPolicy.hasStableTranscript("普通内容", "其他内容", 1_000, 1_500, 450))
        assertFalse(VoiceTranscriptionPolicy.hasStableTranscript("普通内容", "普通内容", 1_000, 1_449, 450))
        assertTrue(VoiceTranscriptionPolicy.hasStableTranscript("普通内容", "普通内容", 1_000, 1_450, 450))
        assertTrue(VoiceTranscriptionPolicy.isVoiceDuration("6\""))
        assertTrue(VoiceTranscriptionPolicy.isKnownFailureText("转文字失败"))
    }

    @Test fun failureDialogRequiresWechatDialogAndExactFailureText() {
        assertTrue(VoiceTranscriptionPolicy.isFailureDialog(
            "com.tencent.mm",
            "com.tencent.mm.ui.widget.dialog.f4",
            listOf("转文字失败"),
        ))
        assertFalse(VoiceTranscriptionPolicy.isFailureDialog(
            "com.other.app",
            "com.tencent.mm.ui.widget.dialog.f4",
            listOf("转文字失败"),
        ))
        assertFalse(VoiceTranscriptionPolicy.isFailureDialog(
            "com.tencent.mm",
            "android.widget.TextView",
            listOf("转文字失败"),
        ))
        assertFalse(VoiceTranscriptionPolicy.isFailureDialog(
            "com.tencent.mm",
            "com.tencent.mm.ui.widget.dialog.f4",
            listOf("提示: 转文字失败, 请重试"),
        ))
        assertFalse(VoiceTranscriptionPolicy.isFailureDialog(
            "com.tencent.mm",
            "com.tencent.mm.ui.widget.dialog.f4",
            emptyList(),
        ))
    }

    @Test fun lockedVoiceAttemptFinalizesOnlyAfterUnlockAndRelock() {
        assertTrue(VoiceTranscriptionPolicy.canFinalizeVoiceAttempt(true, true, true))
        assertFalse(VoiceTranscriptionPolicy.canFinalizeVoiceAttempt(false, true, true))
        assertFalse(VoiceTranscriptionPolicy.canFinalizeVoiceAttempt(true, false, true))
        assertFalse(VoiceTranscriptionPolicy.canFinalizeVoiceAttempt(true, true, false))
    }
}
