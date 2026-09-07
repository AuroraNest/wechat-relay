package com.aurora.wechatrelay.probe

import android.app.Activity
import android.app.AlertDialog
import android.app.NotificationManager
import android.content.ComponentName
import android.content.ClipboardManager
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.os.Bundle
import android.os.UserManager
import android.provider.Settings
import android.service.notification.NotificationListenerService
import android.text.InputType
import android.view.View
import android.view.WindowManager
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import java.io.FileInputStream
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import org.json.JSONObject

class MainActivity : Activity() {
    private lateinit var statusView: TextView
    private lateinit var diagnosticView: TextView
    private lateinit var lockscreenStatusView: TextView
    private lateinit var candidateContainer: LinearLayout
    private var pairingStatus: String? = null
    private var cleanRebindRequested = false
    private var repairInProgress = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val content = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(18), dp(24), dp(18), dp(32))
            setBackgroundColor(Color.rgb(244, 247, 251))
        }
        content.addView(TextView(this).apply {
            text = "微信消息中继"
            textSize = 28f
            typeface = Typeface.DEFAULT_BOLD
            setTextColor(Color.rgb(25, 32, 45))
        })
        content.addView(TextView(this).apply {
            text = "Android 端 · 端到端加密"
            textSize = 14f
            setTextColor(Color.rgb(111, 121, 138))
            setPadding(0, dp(4), 0, dp(18))
        })

        statusView = TextView(this).apply {
            textSize = 15f
            setTextColor(Color.rgb(47, 57, 72))
            setLineSpacing(dp(5).toFloat(), 1f)
        }
        content.addView(card("连接状态", statusView))

        val actions = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        actions.addView(button("刷新状态", primary = true) { refreshUi() })
        actions.addView(button("修复通知连接") {
            requestListenerRebind(ComponentName(this, WechatNotificationListenerService::class.java), force = true)
            statusView.postDelayed({ refreshUi() }, 800)
        })
        actions.addView(button("打开通知读取权限") {
            startActivity(Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS))
        })
        actions.addView(button("重新配对") { showPairingEntry() })
        actions.addView(button("补传语音转写到原会话") { showClipboardSync() })
        actions.addView(button("重试最近失败语音") {
            AlertDialog.Builder(this).setMessage("将重新打开最近失败语音的微信会话并读取转写. 请确认该会话没有更新的语音, 或发送一条新语音触发自动处理.")
                .setNegativeButton("取消", null).setPositiveButton("重试") { _, _ ->
                    WechatNotificationListenerService.requestLatestVoiceRetry(this)
                }.show()
        })
        content.addView(card("连接与修复", actions))

        lockscreenStatusView = TextView(this).apply {
            textSize = 14f
            setTextColor(Color.rgb(47, 57, 72))
            setLineSpacing(dp(4).toFloat(), 1f)
        }
        val lockscreen = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        lockscreen.addView(lockscreenStatusView)
        lockscreen.addView(TextView(this).apply {
            text = "实验功能包含锁屏回复, 新图片同步, 聊天记录自动转发和语音转文字. 新语音通过微信原生转文字同步到 PWA, 不在微信发送消息, 不提供原声. 新收到的聊天记录会通过微信转发到聊天记录中继群, 请先建群并加入接收端微信. 来源或目标不明确时停止, 发送结果不明时不重试. 新图片会在微信查看器中按屏幕显示分辨率截图, 不保存到安卓相册. 截图仅裁剪图片显示区域并加密传给已配对 iPhone. 仅在 Android 16/API 36+ 的 debug build 可启用. 重启后必须先手动解锁一次. PIN 错误或流程异常会自动禁用, 需要重新保存 PIN."
            textSize = 13f
            setTextColor(Color.rgb(111, 121, 138))
            setPadding(0, dp(8), 0, dp(8))
        })
        lockscreen.addView(button("配置并启用 PIN") { showPinEntry() })
        lockscreen.addView(button("打开 Accessibility 设置") {
            startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
        })
        lockscreen.addView(button("停用并删除 PIN") { confirmDisableLockscreenReply() })
        content.addView(card("锁屏 Accessibility 回复实验", lockscreen))

        diagnosticView = TextView(this).apply {
            textSize = 13f
            typeface = Typeface.MONOSPACE
            setTextColor(Color.rgb(65, 76, 94))
            setLineSpacing(dp(4).toFloat(), 1f)
            setTextIsSelectable(true)
        }
        val diagnostics = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        diagnostics.addView(diagnosticView)
        diagnostics.addView(button("导出脱敏日志") { exportJsonl() })
        content.addView(card("回复诊断", diagnostics))

        candidateContainer = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        val advanced = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        advanced.addView(TextView(this).apply {
            text = "仅用于检查微信通知是否提供系统回复入口."
            textSize = 13f
            setTextColor(Color.rgb(111, 121, 138))
            setPadding(0, 0, 0, dp(8))
        })
        advanced.addView(button("扫描当前微信回复入口") {
            WechatNotificationListenerService.requestCandidateScan(this)
            statusView.postDelayed({ refreshUi() }, 350)
        })
        advanced.addView(candidateContainer)
        content.addView(card("高级测试", advanced))

        setContentView(ScrollView(this).apply {
            isFillViewport = true
            addView(content)
        })
    }

    private fun showClipboardSync() {
        // Clipboard access must originate from this focused Activity, not the background listener.
        if (!hasWindowFocus()) return
        val clip = runCatching { getSystemService(ClipboardManager::class.java).primaryClip }.getOrNull()
        val text = clip?.takeIf { it.itemCount == 1 }?.getItemAt(0)?.text?.toString()
        if (text == null || VoiceResultMessages.clipboardPreview("语音", text) == null) {
            AlertDialog.Builder(this).setMessage("请先复制一段非空短文字. 超出单条长度的内容不会截断发送.")
                .setPositiveButton("知道了", null).show()
            return
        }
        Thread {
            val choices = runCatching {
                val store = SyncStore.get(this)
                val deviceId = store.deviceId()
                store.failedVoiceTasks(deviceId).map { task ->
                    task to JSONObject(store.historyTaskPayload(deviceId, task)).getString("sender")
                }
            }.getOrDefault(emptyList())
            runOnUiThread {
                if (isFinishing || isDestroyed) return@runOnUiThread
                if (choices.isEmpty()) {
                    AlertDialog.Builder(this).setMessage("没有可补传的失败语音任务, 请检查配对或等待新语音.")
                        .setPositiveButton("知道了", null).show()
                } else {
                    val labels = choices.map { (task, sender) ->
                        "$sender · ${if (task.wechatUserId == 0) "主微信" else "分身微信"} · ${SimpleDateFormat("MM-dd HH:mm:ss", Locale.ROOT).format(Date(task.postTime))}"
                    }.toTypedArray()
                    AlertDialog.Builder(this).setTitle("选择这段文字对应的原语音")
                        .setItems(labels) { _, index ->
                            val (task, sender) = choices[index]
                            confirmClipboardSync(task, sender, text)
                        }.setNegativeButton("取消", null).show()
                }
            }
        }.start()
    }

    private fun confirmClipboardSync(task: HistoryForwardTask, sender: String, text: String) {
        val preview = VoiceResultMessages.clipboardPreview(sender, text) ?: return
        AlertDialog.Builder(this).setTitle("补传到 iPhone 的 $sender 会话").setMessage(text)
            .setNegativeButton("取消", null)
            .setPositiveButton("同步") { _, _ ->
                Thread {
                    val message = runCatching {
                        val store = SyncStore.get(this)
                        check(store.paired())
                        val deviceId = task.deviceId
                        check(store.isCurrentDevice(deviceId))
                        val id = SyncProtocol.uuidV7()
                        val seq = store.nextSeq(deviceId)
                        val createdAt = System.currentTimeMillis()
                        val encryption = store.messageEncryptionContext(deviceId)
                        val envelope = try {
                            SyncProtocol.encrypt(encryption.a2iKey, id, deviceId, seq, createdAt, preview, task.wechatUserId)
                        } finally { encryption.a2iKey.fill(0) }
                        check(store.enqueue(deviceId, id, seq, createdAt, SyncProtocol.envelopeJson(envelope), wechatUserId = task.wechatUserId))
                        SyncNetwork.enqueue(this)
                        "文字已加密入队, 正在同步到 iPhone."
                    }.getOrElse { "同步未能入队, 请检查配对与队列状态. 剪贴板内容仍保留." }
                    runOnUiThread {
                        if (!isFinishing && !isDestroyed) {
                            AlertDialog.Builder(this).setMessage(message).setPositiveButton("知道了", null).show()
                            refreshUi()
                        }
                    }
                }.start()
            }.show()
    }

    override fun onResume() {
        super.onResume()
        WechatNotificationListenerService.requestReplyPolling(this)
        refreshUi()
    }

    @Deprecated("Deprecated in Java")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode != ExportRequest || resultCode != RESULT_OK) return
        val destination = data?.data ?: return
        val source = ProbeStore(this).file()
        if (!source.exists()) return
        contentResolver.openOutputStream(destination)?.use { output ->
            FileInputStream(source).use { input -> input.copyTo(output) }
        }
    }

    private fun refreshUi() {
        val notificationManager = getSystemService(NotificationManager::class.java)
        val listener = ComponentName(this, WechatNotificationListenerService::class.java)
        val granted = notificationManager.isNotificationListenerAccessGranted(listener)
        if (granted) requestListenerRebind(listener)
        val paired = SyncStore.get(this).paired()
        val relayActive = SyncStore.get(this).relayPolicy().active()
        val lastCapture = ProbeRuntime.lastCaptureMillis?.let(::formatTime) ?: "本次启动后暂无"
        val pinStatus = LockscreenPinStore(this).status(
            accessibilityLive = LockscreenAccessibilityReplyService.isLive(),
            userUnlocked = getSystemService(UserManager::class.java).isUserUnlocked,
        )
        statusView.text = """
            加密连接: ${if (paired) "已配对" else "未配对"}
            通知权限: ${if (granted) "已授权" else "未授权"}
            通知监听: ${if (ProbeRuntime.listenerConnected) "已连接" else "未连接"}
            回复轮询: ${ProbeRuntime.lastReplyStatus}
            消息转发: ${if (relayActive) "已开启" else "已暂停"}
            最近通知: $lastCapture
            自动处理队列: ${ProbeRuntime.historyQueueStatus}
            ${pairingStatus.orEmpty()}
        """.trimIndent()
        lockscreenStatusView.text = """
            Build Flag: ${if (pinStatus.buildEnabled) "debug 已启用" else "release 已关闭"}
            Android API: ${if (pinStatus.platformEligible) "36+ 可用" else "低于 36, 已关闭"}
            PIN: ${if (pinStatus.pinStored) "已加密保存" else "未配置"}
            Accessibility: ${if (pinStatus.accessibilityLive) "服务已连接" else "服务未连接"}
            锁屏回复: ${if (pinStatus.armed) "已 armed" else "未 armed"}
            ${if (pinStatus.attemptBlocked) "安全锁止: 必须重新保存 PIN." else ""}
        """.trimIndent()

        val store = ProbeStore(this)
        diagnosticView.text = buildString {
            append("当前: ${ProbeRuntime.lastDiagnostic}\n")
            append("脱敏事件: ${store.eventCount()} 条\n")
            val diagnostics = store.recentDiagnostics()
            if (diagnostics.isEmpty()) append("最近记录: 暂无")
            else diagnostics.forEach { append("\n${diagnosticLine(it)}") }
        }

        candidateContainer.removeAllViews()
        if (ProbeRuntime.remoteInputCandidates.isEmpty()) {
            candidateContainer.addView(TextView(this).apply {
                text = "当前没有可用的微信系统回复入口."
                textSize = 13f
                setTextColor(Color.rgb(111, 121, 138))
            })
            return
        }
        ProbeRuntime.remoteInputCandidates.forEach { candidate ->
            candidateContainer.addView(button(
                "测试入口 ${candidate.actionIndex + 1} · 输入项 ${candidate.remoteInputCount}",
            ) { showReplyEntry(candidate) })
        }
    }

    private fun showPairingEntry() {
        val code = EditText(this).apply {
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS
            hint = "粘贴 AWR1: 开头的配对码"
            minLines = 4
        }
        AlertDialog.Builder(this)
            .setTitle("连接 iPhone")
            .setMessage("配对码10分钟内有效. 不要把配对码发到聊天或日志中.")
            .setView(code)
            .setNegativeButton("取消", null)
            .setPositiveButton("配对") { _, _ ->
                Thread {
                    val result = runCatching { SyncNetwork.pair(this, SyncNetwork.parsePairingCode(code.text.toString().trim())) }
                    code.post {
                        code.text?.clear()
                        pairingStatus = when {
                            result.getOrDefault(false) -> {
                                WechatNotificationListenerService.requestReplyPolling(this)
                                "配对完成, 加密双向中继已启用."
                            }
                            result.isFailure -> "本地配对失败: ${result.exceptionOrNull()?.javaClass?.simpleName}."
                            else -> "配对被拒绝, 请检查配对码有效期和网络."
                        }
                        refreshUi()
                    }
                }.start()
            }
            .show()
    }

    private fun showPinEntry() {
        if (!BuildConfig.LOCKSCREEN_ACCESSIBILITY_REPLY || !LockscreenReplyPlatform.isEligible(android.os.Build.VERSION.SDK_INT)) {
            pairingStatus = "仅 Android 16/API 36+ debug build 可启用锁屏回复实验."
            refreshUi()
            return
        }
        val first = securePinField("输入 4-16 位数字 PIN")
        val second = securePinField("再次输入 PIN")
        val fields = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), 0, dp(20), 0)
            addView(first)
            addView(second)
        }
        val dialog = AlertDialog.Builder(this)
            .setTitle("配置锁屏回复 PIN")
            .setMessage("仅保存 AndroidKeyStore AES-256-GCM 密文. 保存即表示明确授权此 debug 实验执行一次锁屏回复流程.")
            .setView(fields)
            .setNegativeButton("取消", null)
            .setPositiveButton("保存并启用") { _, _ ->
                val pin = first.text.toString().toCharArray()
                val confirmation = second.text.toString().toCharArray()
                first.text?.clear()
                second.text?.clear()
                pairingStatus = try {
                    when {
                        !LockscreenPinStore.isValidPin(pin) -> "PIN 必须是 4-16 位 ASCII 数字."
                        !pin.contentEquals(confirmation) -> "两次 PIN 不一致, 未保存."
                        LockscreenPinStore(this).saveAndArm(pin) -> "PIN 已加密保存. Accessibility 连接后才能 armed."
                        else -> "PIN 未保存."
                    }
                } finally {
                    pin.fill('\u0000')
                    confirmation.fill('\u0000')
                }
                refreshUi()
            }
            .create()
        dialog.window?.addFlags(WindowManager.LayoutParams.FLAG_SECURE)
        dialog.setOnDismissListener {
            first.text?.clear()
            second.text?.clear()
            dialog.window?.clearFlags(WindowManager.LayoutParams.FLAG_SECURE)
        }
        dialog.show()
    }

    private fun securePinField(hintText: String): EditText = EditText(this).apply {
        inputType = InputType.TYPE_CLASS_NUMBER or InputType.TYPE_NUMBER_VARIATION_PASSWORD
        hint = hintText
        importantForAutofill = View.IMPORTANT_FOR_AUTOFILL_NO_EXCLUDE_DESCENDANTS
        setSingleLine(true)
    }

    private fun confirmDisableLockscreenReply() {
        AlertDialog.Builder(this)
            .setTitle("停用锁屏回复?")
            .setMessage("将立即取消本地 opt-in, 删除 PIN 密文和专用 AndroidKeyStore key. Accessibility 系统开关需在设置中另行关闭.")
            .setNegativeButton("取消", null)
            .setPositiveButton("停用并删除") { _, _ ->
                val result = LockscreenPinStore(this).disarmAndDelete()
                LockscreenAccessibilityReplyService.abortForDisarm()
                pairingStatus = when {
                    result.localStateDeleted && result.keyDeleted -> "锁屏回复已停用, PIN 密文和专用 key 已删除."
                    result.localStateDeleted -> {
                        ProbeRuntime.lastDiagnostic = "LOCKSCREEN_KEY_DELETE_FAILED"
                        "锁屏回复已停用且 PIN 密文已删除, 但专用 key 删除未确认."
                    }
                    result.keyDeleted -> "专用 key 已删除, 但本地 opt-in/PIN 密文删除未确认."
                    else -> "停用失败: 本地 opt-in/PIN 密文和专用 key 删除均未确认."
                }
                refreshUi()
            }
            .show()
    }

    private fun showReplyEntry(candidate: RemoteInputCandidate) {
        val reply = EditText(this).apply {
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS
            hint = "输入测试回复"
        }
        AlertDialog.Builder(this)
            .setTitle("测试微信系统回复")
            .setMessage("回复内容仅在内存中短暂存在, 不会写入诊断日志.")
            .setView(reply)
            .setNegativeButton("取消", null)
            .setPositiveButton("下一步") { _, _ ->
                val text = reply.text.toString()
                reply.text?.clear()
                if (text.isNotBlank()) showFinalReplyConfirmation(candidate, text)
            }
            .show()
    }

    private fun showFinalReplyConfirmation(candidate: RemoteInputCandidate, replyText: String) {
        AlertDialog.Builder(this)
            .setTitle("确认发送测试回复?")
            .setMessage("将通过当前微信通知的系统回复入口发送一次. 请仅用于已确认的测试账号.")
            .setNegativeButton("取消", null)
            .setPositiveButton("发送一次") { _, _ ->
                WechatNotificationListenerService.requestOneTimeReply(this, candidate, replyText)
                statusView.postDelayed({ refreshUi() }, 500)
            }
            .show()
    }

    private fun exportJsonl() {
        startActivityForResult(
            Intent(Intent.ACTION_CREATE_DOCUMENT)
                .addCategory(Intent.CATEGORY_OPENABLE)
                .setType("application/x-ndjson")
                .putExtra(Intent.EXTRA_TITLE, "微信消息中继-脱敏诊断.jsonl"),
            ExportRequest,
        )
    }

    private fun requestListenerRebind(listener: ComponentName, force: Boolean = false) {
        val repairPreferences = getSharedPreferences(ListenerRepairPreferences, MODE_PRIVATE)
        val lastUpdateTime = packageManager.getPackageInfo(packageName, 0).lastUpdateTime
        if (!force && (cleanRebindRequested || repairPreferences.getLong(LastRepairedUpdateTime, 0L) == lastUpdateTime)) return
        if (repairInProgress) return
        cleanRebindRequested = true
        repairInProgress = true
        try {
            packageManager.setComponentEnabledSetting(
                listener,
                PackageManager.COMPONENT_ENABLED_STATE_DISABLED,
                PackageManager.DONT_KILL_APP,
            )
        } catch (_: Exception) {
            repairInProgress = false
            return
        }
        statusView.postDelayed({
            try {
                packageManager.setComponentEnabledSetting(
                    listener,
                    PackageManager.COMPONENT_ENABLED_STATE_DEFAULT,
                    PackageManager.DONT_KILL_APP,
                )
                NotificationListenerService.requestRebind(listener)
                repairPreferences.edit().putLong(LastRepairedUpdateTime, lastUpdateTime).commit()
            } catch (_: Exception) {
            } finally {
                repairInProgress = false
            }
        }, CleanRebindDelayMillis)
    }

    private fun card(title: String, body: View): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        setPadding(dp(16), dp(15), dp(16), dp(15))
        background = rounded(Color.WHITE, 16)
        elevation = dp(1).toFloat()
        layoutParams = LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT).apply {
            bottomMargin = dp(14)
        }
        addView(TextView(this@MainActivity).apply {
            text = title
            textSize = 17f
            typeface = Typeface.DEFAULT_BOLD
            setTextColor(Color.rgb(25, 32, 45))
            setPadding(0, 0, 0, dp(10))
            isAccessibilityHeading = true
        })
        addView(body)
    }

    private fun button(label: String, primary: Boolean = false, action: () -> Unit): Button = Button(this).apply {
        text = label
        isAllCaps = false
        textSize = 15f
        setTextColor(if (primary) Color.WHITE else Color.rgb(39, 91, 158))
        background = rounded(if (primary) Color.rgb(22, 119, 255) else Color.rgb(237, 244, 255), 12)
        minHeight = dp(48)
        contentDescription = label
        layoutParams = LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT).apply {
            bottomMargin = dp(8)
        }
        setOnClickListener { action() }
    }

    private fun rounded(color: Int, radius: Int) = GradientDrawable().apply {
        shape = GradientDrawable.RECTANGLE
        setColor(color)
        cornerRadius = dp(radius).toFloat()
    }

    private fun diagnosticLine(event: SafeDiagnostic): String {
        val time = event.capturedAt.takeIf { it > 0L }?.let(::formatTime) ?: "--:--:--"
        val label = when (event.stage) {
            "REPLY_POLL_STARTED" -> "回复轮询已启动"
            "REPLY_POLL_FAILED" -> "回复轮询失败"
            "REPLY_COMMAND_RECEIVED" -> "已收到回复任务"
            "REPLY_ACK_SENT" -> "回复结果已确认"
            "REPLY_ACK_FAILED" -> "回复结果确认失败"
            null -> event.status?.let { "微信回复结果: ${replyStatusLabel(it)}" } ?: event.eventType
            else -> event.stage
        }
        val detail = event.detail ?: event.exceptionClass?.substringAfterLast('.')
        return "$time  $label${detail?.let { " · $it" }.orEmpty()}"
    }

    private fun formatTime(value: Long): String = SimpleDateFormat("HH:mm:ss", Locale.CHINA).format(Date(value))

    private fun replyStatusLabel(status: String): String = when (status) {
        "SENT_TO_WECHAT" -> "已交给微信"
        "NOTIFICATION_NOT_ACTIVE" -> "原通知已失效"
        "WECHAT_ACTION_CHANGED" -> "微信回复入口已变化"
        "REMOTE_INPUT_UNSUPPORTED" -> "当前通知不支持回复"
        "PENDING_INTENT_CANCELED" -> "微信已取消回复入口"
        "INVALID_REPLY" -> "回复数据无效"
        "FAILED" -> "发送失败"
        else -> status
    }

    private fun dp(value: Int): Int = (value * resources.displayMetrics.density).toInt()

    private companion object {
        const val ExportRequest = 1001
        const val CleanRebindDelayMillis = 300L
        const val ListenerRepairPreferences = "listener_repair"
        const val LastRepairedUpdateTime = "last_repaired_update_time"
    }
}
