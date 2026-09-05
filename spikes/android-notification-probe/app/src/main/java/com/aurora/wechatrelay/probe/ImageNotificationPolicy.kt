package com.aurora.wechatrelay.probe

object ImageNotificationPolicy {
    fun shouldCapture(preview: Preview, groupSummary: Boolean, ageMillis: Long): Boolean {
        if (groupSummary || ageMillis !in 0..120_000 || preview.sender.isBlank()) return false
        val body = preview.body.trim()
        if (body == "[图片]" || body == "[Image]") return true
        return Regex("(?:\\[\\d+条\\])?" + Regex.escape(preview.sender) + ":\\s*\\[图片\\]").matches(body)
    }
}
