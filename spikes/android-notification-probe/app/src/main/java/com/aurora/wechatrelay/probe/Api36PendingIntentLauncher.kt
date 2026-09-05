package com.aurora.wechatrelay.probe

import android.annotation.TargetApi
import android.app.Activity
import android.app.ActivityOptions
import android.app.PendingIntent

@TargetApi(36)
object Api36PendingIntentLauncher {
    fun send(activity: Activity, pendingIntent: PendingIntent) {
        val options = ActivityOptions.makeBasic().apply {
            pendingIntentBackgroundActivityStartMode = ActivityOptions.MODE_BACKGROUND_ACTIVITY_START_ALLOW_IF_VISIBLE
        }
        pendingIntent.send(activity, 0, null, null, null, null, options.toBundle())
    }
}
