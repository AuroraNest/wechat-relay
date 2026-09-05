package com.aurora.wechatrelay.probe

import android.content.Context
import java.io.File
import org.json.JSONObject

data class SafeDiagnostic(
    val capturedAt: Long,
    val eventType: String,
    val stage: String?,
    val status: String?,
    val exceptionClass: String?,
    val detail: String?,
)

class ProbeStore(context: Context) {
    private val eventFile = File(context.filesDir, "redacted-notification-events.jsonl")

    @Synchronized
    fun append(line: String) {
        eventFile.appendText(line + "\n", Charsets.UTF_8)
    }

    fun file(): File = eventFile

    fun eventCount(): Int = if (!eventFile.exists()) 0 else eventFile.useLines { it.count() }

    fun recentDiagnostics(limit: Int = 8): List<SafeDiagnostic> {
        if (!eventFile.exists()) return emptyList()
        val recent = ArrayDeque<String>()
        eventFile.useLines { lines ->
            lines.forEach { line ->
                val type = runCatching { JSONObject(line).optString("eventType") }.getOrNull()
                if (type != "syncDiagnostic" && type != "remoteInputTestResult") return@forEach
                recent.addLast(line)
                if (recent.size > limit) recent.removeFirst()
            }
        }
        return recent.mapNotNull { line ->
            runCatching {
                val event = JSONObject(line)
                SafeDiagnostic(
                    capturedAt = event.optLong("capturedAt", 0L),
                    eventType = event.optString("eventType"),
                    stage = event.optString("stage").takeIf(String::isNotBlank),
                    status = event.optString("status").takeIf(String::isNotBlank),
                    exceptionClass = event.optString("exceptionClass").takeIf(String::isNotBlank),
                    detail = event.optString("detail").takeIf(String::isNotBlank),
                )
            }.getOrNull()
        }.reversed()
    }
}
