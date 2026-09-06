package com.aurora.wechatrelay.probe

internal object ChatHistoryForwardPolicy {
    const val TargetGroup = "聊天记录中继"

    fun isForwardTranslationNotice(title: CharSequence?, message: CharSequence?, action: CharSequence?): Boolean =
        title?.toString() == "提示" && message?.toString() == "聊天记录被转发时不会包含翻译内容\u3002" && action?.toString() == "知道了"

    fun matchesChatTitle(visible: CharSequence?, expected: String): Boolean {
        val title = LockscreenReplySelectors.normalizeTitle(visible)
        return expected.isNotBlank() && (title == expected || Regex(Regex.escape(expected) + "\\([1-9]\\d*人?\\)").matches(title))
    }

    fun sourceTitleMatches(titles: List<String>, conversationTitle: String, sender: String): Boolean =
        if (titles.isEmpty()) sender == conversationTitle
        else titles.size == 1 && matchesChatTitle(titles.single(), conversationTitle)

    fun shouldForward(preview: Preview, conversationTitle: String, groupSummary: Boolean, ageMillis: Long): Boolean {
        if (groupSummary || ageMillis !in 0..120_000 || preview.sender.isBlank() || conversationTitle.isBlank()) return false
        if (matchesChatTitle(conversationTitle, TargetGroup)) return false
        return notificationBody(preview) == "[聊天记录]"
    }

    fun latestHistoryIndex(historyFlags: List<Boolean>, cardsAfter: Int = 0): Int? =
        if (cardsAfter < 0) null else historyFlags.indices.filter { historyFlags[it] }.dropLast(cardsAfter).lastOrNull()

    fun notificationBody(preview: Preview): String {
        var body = preview.body.trim()
        body = body.replaceFirst(Regex("^\\[\\d+条\\]"), "")
        val senderPrefix = "${preview.sender}: "
        if (body.startsWith(senderPrefix)) body = body.removePrefix(senderPrefix)
        return body.trim()
    }

    fun missingTexts(captured: List<String>, notified: List<String>): List<IndexedValue<String>> {
        val remainingNotifications = notified.groupingBy { it }.eachCount().toMutableMap()
        return captured.mapIndexedNotNull { index, text ->
            val remaining = remainingNotifications[text] ?: 0
            if (remaining > 0) {
                remainingNotifications[text] = remaining - 1
                null
            } else {
                IndexedValue(index, text)
            }
        }
    }

    fun canFinalizeHistoryAttempt(startedLocked: Boolean, unlockSucceeded: Boolean, relocked: Boolean): Boolean =
        startedLocked && unlockSucceeded && relocked
}
