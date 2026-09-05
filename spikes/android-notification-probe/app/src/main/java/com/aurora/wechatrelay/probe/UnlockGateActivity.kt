package com.aurora.wechatrelay.probe

import android.app.Activity
import android.app.KeyguardManager
import android.graphics.Color
import android.os.Bundle
import android.os.SystemClock
import android.view.Gravity
import android.view.WindowManager
import android.widget.TextView
import java.lang.ref.WeakReference

class UnlockGateActivity : Activity() {
    private lateinit var keyguardManager: KeyguardManager
    private var dismissRequested = false
    private var dismissSucceeded = false
    private var launchDelivered = false
    private var gateRecheckDeadline = 0L

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setShowWhenLocked(true)
        setTurnScreenOn(true)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        setContentView(TextView(this).apply {
            setBackgroundColor(Color.BLACK)
            setTextColor(Color.WHITE)
            text = "正在请求系统解锁"
            textSize = 16f
            gravity = Gravity.CENTER
            isFocusableInTouchMode = true
            requestFocus()
        })
        keyguardManager = getSystemService(KeyguardManager::class.java)
        current = WeakReference(this)
    }

    override fun onResume() {
        super.onResume()
        current = WeakReference(this)
        requestDismissWhenVisible()
    }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        if (hasFocus) {
            LockscreenAccessibilityReplyService.onGateFocused()
            requestDismissWhenVisible()
            maybeLaunchWechat()
        }
    }

    private fun requestDismissWhenVisible() {
        if (!UnlockGatePolicy.shouldRequestDismiss(hasWindowFocus(), dismissRequested, dismissSucceeded)) return
        if (!LockscreenAccessibilityReplyService.requiresGateDismiss()) {
            dismissSucceeded = true
            maybeLaunchWechat()
            return
        }
        val requested = LockscreenAccessibilityReplyService.runGateAction {
            keyguardManager.requestDismissKeyguard(this, object : KeyguardManager.KeyguardDismissCallback() {
                override fun onDismissSucceeded() {
                    dismissSucceeded = true
                    LockscreenAccessibilityReplyService.onGateDismissSucceeded()
                    maybeLaunchWechat()
                }

                override fun onDismissCancelled() {
                    LockscreenAccessibilityReplyService.onGateFailure("KEYGUARD_DISMISS_CANCELLED")
                }

                override fun onDismissError() {
                    LockscreenAccessibilityReplyService.onGateFailure("KEYGUARD_DISMISS_ERROR")
                }
            })
        }
        if (!requested) {
            finish()
            return
        }
        dismissRequested = true
        gateRecheckDeadline = SystemClock.elapsedRealtime() + GateRecheckTimeoutMillis
        // The native request has been accepted from a focused, visible Activity; only now may
        // Accessibility observe and enter the system credential UI.
        LockscreenAccessibilityReplyService.onGateDismissRequested()
        maybeLaunchWechat()
    }

    private fun maybeLaunchWechat() {
        if (launchDelivered || !hasWindowFocus()) return
        if (dismissSucceeded || !keyguardManager.isDeviceLocked) {
            launchDelivered = LockscreenAccessibilityReplyService.onGateUnlocked(this)
        }
        if (!launchDelivered && SystemClock.elapsedRealtime() < gateRecheckDeadline) {
            window.decorView.postDelayed(::maybeLaunchWechat, GateRecheckMillis)
        }
    }

    companion object {
        private const val GateRecheckMillis = 100L
        private const val GateRecheckTimeoutMillis = 15_000L
        @Volatile private var current: WeakReference<UnlockGateActivity>? = null

        fun recheck() {
            current?.get()?.runOnUiThread { current?.get()?.maybeLaunchWechat() }
        }

        fun closeGate() {
            current?.get()?.runOnUiThread {
                current?.get()?.finish()
                current = null
            }
        }
    }
}

internal object UnlockGatePolicy {
    fun shouldRequestDismiss(hasWindowFocus: Boolean, dismissRequested: Boolean, dismissSucceeded: Boolean): Boolean =
        hasWindowFocus && !dismissRequested && !dismissSucceeded
}
