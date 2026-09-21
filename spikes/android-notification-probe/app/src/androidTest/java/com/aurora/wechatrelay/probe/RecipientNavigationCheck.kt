package com.aurora.wechatrelay.probe

import android.accessibilityservice.AccessibilityServiceInfo
import android.app.Instrumentation
import android.app.UiAutomation
import android.os.Bundle
import android.os.SystemClock
import android.view.accessibility.AccessibilityNodeInfo
import android.view.accessibility.AccessibilityEvent
import java.util.concurrent.CopyOnWriteArrayList

// Run manually from an unlocked WeChat chat. This check never edits or sends a message.
class RecipientNavigationCheck : Instrumentation() {
    override fun onCreate(arguments: Bundle?) {
        super.onCreate(arguments)
        start()
    }

    override fun onStart() {
        val result = Bundle()
        try {
            val automation = requireNotNull(getUiAutomation(UiAutomation.FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES))
            automation.serviceInfo = automation.serviceInfo.apply {
                flags = flags or AccessibilityServiceInfo.FLAG_REPORT_VIEW_IDS or
                    AccessibilityServiceInfo.FLAG_INCLUDE_NOT_IMPORTANT_VIEWS
            }
            fun nodes(): List<AccessibilityNodeInfo> {
                val root = requireNotNull(automation.rootInActiveWindow)
                check(root.packageName?.toString() == "com.tencent.mm") { "WECHAT_NOT_FOREGROUND" }
                val pending = ArrayDeque<AccessibilityNodeInfo>().apply { add(root) }
                val all = mutableListOf<AccessibilityNodeInfo>()
                while (pending.isNotEmpty()) {
                    val node = pending.removeFirst()
                    all += node
                    for (i in 0 until node.childCount) node.getChild(i)?.let(pending::add)
                }
                return all.filter { it.isVisibleToUser }
            }
            fun awaitNode(id: String): AccessibilityNodeInfo {
                repeat(30) {
                    nodes().filter { it.viewIdResourceName == id && (id != "com.tencent.mm:id/bkk" || it.isEditable) }
                        .singleOrNull()?.let { return it }
                    SystemClock.sleep(100)
                }
                error("CONTROL_NOT_FOUND:$id matches=${nodes().filter { it.viewIdResourceName == id }.map { node -> "class=${node.className},editable=${node.isEditable},enabled=${node.isEnabled},bounds=${android.graphics.Rect().also(node::getBoundsInScreen)},identity=${node.hashCode()}" }}")
            }
            val input = awaitNode("com.tencent.mm:id/bkk")
            val originalText = input.text?.toString().orEmpty()
            val transitions = CopyOnWriteArrayList<String>()
            automation.setOnAccessibilityEventListener { event ->
                if (event.eventType == AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED || event.eventType == AccessibilityEvent.TYPE_VIEW_CLICKED) {
                    transitions += "${event.packageName}:${event.eventType}:${event.className}:${event.source?.viewIdResourceName}"
                }
            }
            val titleLabels = nodes().filter {
                android.graphics.Rect().also(it::getBoundsInScreen).top < targetContext.resources.displayMetrics.heightPixels * 0.45
            }.map { listOfNotNull(it.text, it.contentDescription).map(CharSequence::toString).distinct() }
            check(awaitNode("com.tencent.mm:id/fq").performAction(AccessibilityNodeInfo.ACTION_CLICK))
            val member = awaitNode("com.tencent.mm:id/m7b")
            check(!member.text.isNullOrBlank()) { "MEMBER_NAME_EMPTY" }
            val titleMatches = titleLabels.count { member.text.toString() in it }
            check(nodes().count { it.viewIdResourceName == "com.tencent.mm:id/m7b" } == 1)
            check(nodes().any { it.viewIdResourceName == "android:id/text1" && it.text?.toString() == "聊天信息" })
            check(awaitNode("com.tencent.mm:id/actionbar_up_indicator").performAction(AccessibilityNodeInfo.ACTION_CLICK))
            val returned = awaitNode("com.tencent.mm:id/bkk")
            check(returned == input) { "COMPOSER_IDENTITY_CHANGED" }
            check(returned.text?.toString().orEmpty() == originalText) { "COMPOSER_TEXT_CHANGED" }
            result.putString("result", "PASS: unique member, same composer, text unchanged, no send; original title matches=$titleMatches; transitions=$transitions")
            finish(-1, result)
        } catch (failure: Exception) {
            result.putString("result", "FAIL: ${failure.javaClass.simpleName}: ${failure.message}")
            finish(0, result)
        }
    }
}
