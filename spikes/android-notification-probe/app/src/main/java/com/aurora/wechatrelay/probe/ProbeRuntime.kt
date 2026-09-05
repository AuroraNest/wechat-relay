package com.aurora.wechatrelay.probe

data class RemoteInputCandidate(
    val sbnKey: String,
    val wechatUserId: Int,
    val displayKeyHash: String,
    val actionIndex: Int,
    val remoteInputCount: Int,
)

data class InMemoryReplyRequest(val candidate: RemoteInputCandidate, val replyText: CharArray)

object ProbeRuntime {
    @Volatile var listenerConnected: Boolean = false
    @Volatile var accessibilityConnected: Boolean = false
    @Volatile var lastCaptureMillis: Long? = null
    @Volatile var lastReplyStatus: String = "尚未启动"
    @Volatile var lastDiagnostic: String = "暂无异常"
    @Volatile var remoteInputCandidates: List<RemoteInputCandidate> = emptyList()

    private var replyUsed = false
    private var pendingReply: InMemoryReplyRequest? = null

    @Synchronized
    fun submitOneTimeReply(candidate: RemoteInputCandidate, replyText: String): Boolean {
        if (replyUsed || pendingReply != null || replyText.isEmpty()) return false
        replyUsed = true
        pendingReply = InMemoryReplyRequest(candidate, replyText.toCharArray())
        return true
    }

    @Synchronized
    fun takeOneTimeReply(): InMemoryReplyRequest? = pendingReply.also { pendingReply = null }
}
