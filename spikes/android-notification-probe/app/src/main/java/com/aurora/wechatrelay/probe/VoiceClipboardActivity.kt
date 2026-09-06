package com.aurora.wechatrelay.probe

import android.app.Activity
import android.app.KeyguardManager
import android.content.ClipDescription
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.graphics.Color
import android.os.Bundle
import android.view.Gravity
import android.view.WindowManager
import android.widget.TextView
import java.lang.ref.WeakReference

class VoiceClipboardActivity : Activity() {
    private var receiptToken = MissingReceiptToken

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        receiptToken = intent.getLongExtra(ExtraReceiptToken, MissingReceiptToken)
        window.addFlags(WindowManager.LayoutParams.FLAG_SECURE)
        setContentView(TextView(this).apply {
            setBackgroundColor(Color.rgb(244, 247, 251))
            setTextColor(Color.rgb(65, 76, 94))
            gravity = Gravity.CENTER
            text = "正在同步语音文字"
            textSize = 16f
        })
        currentActivity = WeakReference(this)
        if (receiptToken == MissingReceiptToken) finish()
    }

    override fun onResume() {
        super.onResume()
        receiveClipboardIfReady()
    }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        if (hasFocus) receiveClipboardIfReady()
    }

    override fun onDestroy() {
        clearPendingIfOwned(receiptToken)
        if (currentActivity?.get() === this) currentActivity = null
        super.onDestroy()
    }

    private fun receiveClipboardIfReady() {
        if (!hasWindowFocus() || getSystemService(KeyguardManager::class.java).isDeviceLocked) return
        val receipt = pendingReceipt(receiptToken) ?: run {
            finish()
            return
        }
        if (!isCurrentSafely(receipt.isCurrent)) {
            clearPendingIfOwned(receiptToken)
            finish()
            return
        }

        val clip = runCatching { getSystemService(ClipboardManager::class.java).primaryClip }.getOrNull()
        val text = clip?.let {
            VoiceClipboardPolicy.acceptedText(
                itemCount = it.itemCount,
                hasPlainTextMimeType = it.description.hasMimeType(ClipDescription.MIMETYPE_TEXT_PLAIN),
                timestampMillis = it.description.timestamp,
                copiedAfterMillis = receipt.copiedAfterMillis,
                nowMillis = System.currentTimeMillis(),
                text = it.takeIf { clipData -> clipData.itemCount == 1 }?.getItemAt(0)?.text,
            )
        }
        completeReceipt(receipt, text)
    }

    private fun completeReceipt(receipt: PendingReceipt, text: String?) {
        val deliver = synchronized(receiptLock) {
            val current = pending?.takeIf { it.token == receipt.token } ?: return@synchronized null
            if (!isCurrentSafely(current.isCurrent)) {
                pending = null
                null
            } else {
                pending = null
                current
            }
        }
        if (deliver != null && isCurrentSafely(deliver.isCurrent)) deliver.completion(text)
        finish()
    }

    companion object {
        private const val ExtraReceiptToken = "voice_clipboard_receipt_token"
        private const val MissingReceiptToken = Long.MIN_VALUE
        private val receiptLock = Any()
        private var nextReceiptToken = 0L
        private var pending: PendingReceipt? = null
        @Volatile private var currentActivity: WeakReference<VoiceClipboardActivity>? = null

        private data class PendingReceipt(
            val token: Long,
            val copiedAfterMillis: Long,
            val isCurrent: () -> Boolean,
            val completion: (String?) -> Unit,
        )

        fun start(
            context: Context,
            copiedAfterMillis: Long,
            isCurrent: () -> Boolean,
            completion: (String?) -> Unit,
        ) {
            val token = synchronized(receiptLock) {
                nextReceiptToken += 1L
                PendingReceipt(nextReceiptToken, copiedAfterMillis, isCurrent, completion).also { pending = it }.token
            }
            currentActivity?.get()?.finish()
            try {
                context.startActivity(Intent(context, VoiceClipboardActivity::class.java).apply {
                    addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                    putExtra(ExtraReceiptToken, token)
                })
            } catch (_: Exception) {
                val receipt = takePendingIfOwned(token)
                if (receipt != null && isCurrentSafely(receipt.isCurrent)) receipt.completion(null)
            }
        }

        fun cancel() {
            synchronized(receiptLock) { pending = null }
            currentActivity?.get()?.finish()
        }

        private fun pendingReceipt(token: Long): PendingReceipt? = synchronized(receiptLock) {
            pending?.takeIf { it.token == token }
        }

        private fun takePendingIfOwned(token: Long): PendingReceipt? = synchronized(receiptLock) {
            pending?.takeIf { it.token == token }?.also { pending = null }
        }

        private fun clearPendingIfOwned(token: Long) {
            synchronized(receiptLock) {
                if (pending?.token == token) pending = null
            }
        }

        private fun isCurrentSafely(isCurrent: () -> Boolean): Boolean =
            runCatching(isCurrent).getOrDefault(false)
    }
}
