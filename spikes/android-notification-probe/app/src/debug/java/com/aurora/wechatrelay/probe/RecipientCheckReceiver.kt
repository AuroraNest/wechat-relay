package com.aurora.wechatrelay.probe

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import org.json.JSONObject

// ADB-only diagnostic: requires an unlocked chat and stops before touching its draft.
class RecipientCheckReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val title = intent.getStringExtra("title")?.takeIf { it.isNotBlank() } ?: return
        val pending = goAsync()
        Thread {
            try {
                val result = LockscreenAccessibilityReplyService.checkCurrentConversation(title)
                ProbeStore(context).append(JSONObject().put("eventType", "syncDiagnostic")
                    .put("capturedAt", System.currentTimeMillis()).put("stage", "RECIPIENT_CHECK_RESULT")
                    .put("status", result.status).put("resultStage", result.stage).toString())
            } finally {
                pending.finish()
            }
        }.start()
    }
}
