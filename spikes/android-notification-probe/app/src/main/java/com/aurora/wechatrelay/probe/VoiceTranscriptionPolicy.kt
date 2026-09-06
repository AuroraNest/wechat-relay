package com.aurora.wechatrelay.probe

object VoiceTranscriptionPolicy {
    private const val FreshnessMillis = 120_000L
    private val NotificationBody = Regex("\\[语音](?:\\s+([1-9]\\d{0,2}\\\"))?")
    private const val WeChatPackage = "com.tencent.mm"
    private const val DialogClassPrefix = "com.tencent.mm.ui.widget.dialog."
    private const val TranscriptionFailedText = "转文字失败"

    fun shouldTranscribe(preview: Preview, conversationTitle: String, groupSummary: Boolean, ageMillis: Long): Boolean {
        if (groupSummary || ageMillis !in 0..FreshnessMillis || preview.sender.isBlank() || conversationTitle.isBlank()) return false
        return NotificationBody.matches(ChatHistoryForwardPolicy.notificationBody(preview))
    }

    fun notificationDuration(preview: Preview): String? =
        NotificationBody.matchEntire(ChatHistoryForwardPolicy.notificationBody(preview))?.groups?.get(1)?.value

    fun latestVoiceIndex(voiceFlags: List<Boolean>, voicesAfter: Int = 0): Int? =
        if (voicesAfter < 0) null else voiceFlags.indices.filter { voiceFlags[it] }.dropLast(voicesAfter).lastOrNull()

    data class PinnedVoiceSelection(
        val voicesAfter: Int,
        val selectedIndex: Int,
        val signatures: List<String>,
    )

    fun pinVoiceSelection(signatures: List<String>, voicesAfter: Int): PinnedVoiceSelection? {
        val selectedIndex = latestVoiceIndex(List(signatures.size) { true }, voicesAfter) ?: return null
        return PinnedVoiceSelection(voicesAfter, selectedIndex, signatures)
    }

    fun matchesPinnedVoice(selection: PinnedVoiceSelection, signatures: List<String>, voicesAfter: Int, sameVoiceNode: Boolean = false): Boolean {
        if (selection.voicesAfter != voicesAfter) return false
        val selectedIndex = latestVoiceIndex(List(signatures.size) { true }, voicesAfter) ?: return false
        if (selection.signatures == signatures) return selection.selectedIndex == selectedIndex
        // Expanding a transcript can push earlier rows above the viewport. The selected Android node must survive.
        val clipped = selection.signatures.size - signatures.size
        return sameVoiceNode && clipped > 0 && selection.selectedIndex - clipped == selectedIndex &&
            selection.signatures.drop(clipped) == signatures
    }

    fun isVoiceDuration(text: String): Boolean = Regex("\\d{1,3}\\\"").matches(text.trim())

    fun intersectsViewport(top: Int, bottom: Int, viewportTop: Int, viewportBottom: Int): Boolean =
        top < bottom && viewportTop < viewportBottom && bottom > viewportTop && top < viewportBottom

    fun shouldRequestHome(startedLocked: Boolean, openedWechat: Boolean, ownsForeground: Boolean): Boolean =
        !startedLocked && openedWechat && ownsForeground

    fun isKnownFailureText(text: String): Boolean {
        val normalized = text.trim()
        return normalized in setOf("转文字失败", "无法转文字", "暂不支持转文字", "语音转文字失败")
    }

    fun isFailureDialog(packageName: String?, className: String?, texts: List<String>): Boolean =
        packageName == WeChatPackage && className?.startsWith(DialogClassPrefix) == true &&
            texts.any { it.trim() == TranscriptionFailedText }

    fun hasStableTranscript(current: String?, previous: String?, stableSinceMillis: Long, nowMillis: Long, minimumMillis: Long): Boolean =
        !current.isNullOrBlank() && current == previous && stableSinceMillis > 0L && nowMillis - stableSinceMillis >= minimumMillis

    fun canFinalizeVoiceAttempt(startedLocked: Boolean, unlockedForWorkflow: Boolean, finalized: Boolean): Boolean =
        startedLocked && unlockedForWorkflow && finalized
}
