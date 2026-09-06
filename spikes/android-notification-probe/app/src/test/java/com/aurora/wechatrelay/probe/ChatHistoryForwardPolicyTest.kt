package com.aurora.wechatrelay.probe

import org.junit.Assert.assertFalse
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class ChatHistoryForwardPolicyTest {
    @Test fun acknowledgesOnlyTheKnownTranslationNotice() {
        val message = "聊天记录被转发时不会包含翻译内容\u3002"
        assertTrue(ChatHistoryForwardPolicy.isForwardTranslationNotice("提示", message, "知道了"))
        assertFalse(ChatHistoryForwardPolicy.isForwardTranslationNotice("提示", "其他提示", "知道了"))
        assertFalse(ChatHistoryForwardPolicy.isForwardTranslationNotice("提示", message, "发送"))
        assertFalse(ChatHistoryForwardPolicy.isForwardTranslationNotice(null, message, "知道了"))
    }
    @Test fun onlyFreshHistoryNotificationsOutsideRelayGroupTriggerForwarding() {
        val history = Preview("测试联系人", "[聊天记录]", "text")
        assertTrue(ChatHistoryForwardPolicy.shouldForward(history, "测试联系人", false, 0))
        assertTrue(ChatHistoryForwardPolicy.shouldForward(history.copy(body = "[2条]测试联系人: [聊天记录]"), "测试联系人", false, 120_000))
        for (body in listOf("解决下面聊天记录说的问题", "请看[聊天记录]", "[图片]", "其他人: [聊天记录]")) {
            assertFalse(ChatHistoryForwardPolicy.shouldForward(history.copy(body = body), "测试联系人", false, 0))
        }
        assertFalse(ChatHistoryForwardPolicy.shouldForward(history, " 聊天记录中继 ", false, 0))
        assertFalse(ChatHistoryForwardPolicy.shouldForward(history, "聊天记录中继(2)", false, 0))
        assertFalse(ChatHistoryForwardPolicy.shouldForward(history, "", false, 0))
        assertFalse(ChatHistoryForwardPolicy.shouldForward(history, "测试联系人", true, 0))
        assertFalse(ChatHistoryForwardPolicy.shouldForward(history, "测试联系人", false, -1))
        assertFalse(ChatHistoryForwardPolicy.shouldForward(history, "测试联系人", false, 120_001))
        assertTrue(ChatHistoryForwardPolicy.matchesChatTitle("聊天记录中继(2)", ChatHistoryForwardPolicy.TargetGroup))
        assertTrue(ChatHistoryForwardPolicy.matchesChatTitle("聊天记录中继(2人)", ChatHistoryForwardPolicy.TargetGroup))
        assertFalse(ChatHistoryForwardPolicy.matchesChatTitle("聊天记录中继备用(2)", ChatHistoryForwardPolicy.TargetGroup))
        assertFalse(ChatHistoryForwardPolicy.matchesChatTitle("聊天记录中继(abc)", ChatHistoryForwardPolicy.TargetGroup))
        assertTrue(ChatHistoryForwardPolicy.sourceTitleMatches(emptyList(), "测试联系人", "测试联系人"))
        assertFalse(ChatHistoryForwardPolicy.sourceTitleMatches(emptyList(), "测试群", "测试联系人"))
        assertFalse(ChatHistoryForwardPolicy.sourceTitleMatches(listOf("其他联系人"), "测试联系人", "测试联系人"))
        assertFalse(ChatHistoryForwardPolicy.sourceTitleMatches(listOf("测试联系人", "其他联系人"), "测试联系人", "测试联系人"))
    }

    @Test fun latestHistoryIndexUsesTheNewestHistoryRecordAmongMessageRows() {
        assertEquals(0, ChatHistoryForwardPolicy.latestHistoryIndex(listOf(true, false)))
        assertEquals(1, ChatHistoryForwardPolicy.latestHistoryIndex(listOf(false, true)))
        assertEquals(0, ChatHistoryForwardPolicy.latestHistoryIndex(listOf(true, false, true), 1))
        assertEquals(null, ChatHistoryForwardPolicy.latestHistoryIndex(listOf(true), 1))
        assertEquals(null, ChatHistoryForwardPolicy.latestHistoryIndex(listOf(true), -1))
        assertEquals(null, ChatHistoryForwardPolicy.latestHistoryIndex(listOf(false, false, false)))
    }

    @Test fun notificationBodyRemovesOnlyExactSenderPrefix() {
        val preview = Preview("测试联系人", "[2条]测试联系人: 正文", "text")
        assertEquals("正文", ChatHistoryForwardPolicy.notificationBody(preview))
        assertEquals("其他人: 正文", ChatHistoryForwardPolicy.notificationBody(preview.copy(body = "[2条]其他人: 正文")))
        assertEquals("测试联系人:正文", ChatHistoryForwardPolicy.notificationBody(preview.copy(body = "测试联系人:正文")))
        assertEquals("任意普通文本", ChatHistoryForwardPolicy.notificationBody(preview.copy(body = " 任意普通文本 ")))
    }

    @Test fun missingTextsKeepsTrailingOrdinaryTextsAndAbsoluteIndices() {
        assertEquals(
            listOf(IndexedValue(1, "尾随文本")),
            ChatHistoryForwardPolicy.missingTexts(listOf("[聊天记录]", "尾随文本"), listOf("[聊天记录]"))
        )
        assertEquals(
            listOf(IndexedValue(0, "前置文本")),
            ChatHistoryForwardPolicy.missingTexts(listOf("前置文本", "[聊天记录]"), listOf("[聊天记录]"))
        )
        assertEquals(
            listOf(IndexedValue(0, "[图片]"), IndexedValue(1, "尾随文本")),
            ChatHistoryForwardPolicy.missingTexts(listOf("[图片]", "尾随文本"), emptyList())
        )
        assertEquals(
            listOf(IndexedValue(1, "重复文本")),
            ChatHistoryForwardPolicy.missingTexts(listOf("重复文本", "重复文本"), listOf("重复文本"))
        )
    }

    @Test fun historyAttemptFinalizesOnlyAfterConfirmedRelock() {
        assertTrue(ChatHistoryForwardPolicy.canFinalizeHistoryAttempt(true, true, true))
        assertFalse(ChatHistoryForwardPolicy.canFinalizeHistoryAttempt(false, true, true))
        assertFalse(ChatHistoryForwardPolicy.canFinalizeHistoryAttempt(true, false, true))
        assertFalse(ChatHistoryForwardPolicy.canFinalizeHistoryAttempt(true, true, false))
    }
}
