package com.aurora.wechatrelay.probe

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.AccessibilityServiceInfo
import android.accessibilityservice.GestureDescription
import android.app.Activity
import android.app.KeyguardManager
import android.app.PendingIntent
import android.content.Intent
import android.content.pm.LauncherApps
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Paint
import android.graphics.Path
import android.graphics.Rect
import android.os.Bundle
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.os.UserManager
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import org.json.JSONObject

data class AccessibilityReplyResult(
    val status: String,
    val stage: String,
    val media: MediaCandidate? = null,
    val startedLocked: Boolean = false,
    val transcript: String? = null,
)

class LockscreenAccessibilityReplyService : AccessibilityService() {
    private val handler = Handler(Looper.getMainLooper())
    private val pinStore by lazy { LockscreenPinStore(this) }
    private val pinTick = Runnable { session?.let(::clickNextPinDigit) }
    private val historyTick = Runnable { session?.let(::processWechatWindow) }
    private val contactsTick = Runnable { contactScan?.let(::processContactsScan) }
    @Volatile private var session: Session? = null
    @Volatile private var contactScan: ContactScan? = null

    private enum class Phase {
        Unlocking, FindingSearch, EnteringSearch, SelectingResult, OpeningWechat,
        OpeningImageViewer, WaitingOriginalView,
        HistoryViewer, HistoryMenu, HistorySearch, HistoryResult, HistoryConfirm, HistoryClaiming, HistoryVerify, HistoryReturnSource,
        VoiceMenu, VoiceWaitingTranscript, VoiceLongPressing, VoiceCopyMenu, VoiceClipboardReading,
        ReadyToClick, Verifying, Capturing, Finishing,
    }

    private data class ImageSourceSelection(
        val windowId: Int,
        val rootBounds: CaptureRect,
        val selection: ImageCaptureSelection,
    )

    private data class ImageProcessingResult(val media: MediaCandidate? = null, val failureStage: String? = null)

    private data class ViewerCaptureSelection(
        val windowId: Int,
        val rootBounds: CaptureRect,
        val viewerBounds: CaptureRect,
        val expectedImageBounds: CaptureRect,
    )

    private data class VoiceSourceSelection(
        val windowId: Int,
        val identity: String,
        val selection: VoiceTranscriptionPolicy.PinnedVoiceSelection,
        val row: AccessibilityNodeInfo,
        val voice: AccessibilityNodeInfo,
        val viewport: Rect,
    )

    private data class VoiceRowMarker(
        val signature: String,
        val row: AccessibilityNodeInfo,
        val voice: AccessibilityNodeInfo,
        val avatar: AccessibilityNodeInfo,
    )

    private data class Session(
        val contentIntent: PendingIntent?,
        val expectedTitleHash: String,
        val conversationTitle: CharArray,
        val wechatUserId: Int,
        val wechatUserSerial: Long,
        val replyText: CharArray?,
        val imageSender: CharArray?,
        val imageIsCurrent: (() -> Boolean)?,
        val historySender: String?,
        val historyIsCurrent: (() -> Boolean)?,
        val historyClaimSend: (() -> Boolean)?,
        val historyCardsAfter: (() -> Int)?,
        val historyCaptureTexts: ((List<String>) -> Unit)?,
        val voiceSender: String?,
        val voiceExpectedDuration: String?,
        val voiceIsCurrent: (() -> Boolean)?,
        val voiceVoicesAfter: (() -> Int)?,
        val voiceCaptureTexts: ((List<String>) -> Unit)?,
        val pin: CharArray,
        val startedLocked: Boolean,
        val completion: CountDownLatch,
        val cancellation: ReplyCancellation = ReplyCancellation(),
        @Volatile var phase: Phase = Phase.Unlocking,
        var initialForegroundPackage: String? = null,
        var gateFocusedLogged: Boolean = false,
        var gateDismissRequested: Boolean = false,
        var dismissRequestedLogged: Boolean = false,
        var dismissSucceededLogged: Boolean = false,
        var keypadWaitingLogged: Boolean = false,
        var pinTimeoutSnapshotLogged: Boolean = false,
        var pinEntryStarted: Boolean = false,
        var inputObservationDeadlineMillis: Long = 0L,
        var setTextAcceptedLogged: Boolean = false,
        var inputTextObservedLogged: Boolean = false,
        var inputTextObservationTimeoutLogged: Boolean = false,
        var nextPinIndex: Int = 0,
        var clickAt: Long = 0L,
        var stableImageSource: ImageSourceSelection? = null,
        var imageStableSinceMillis: Long = 0L,
        var screenshotStarted: Boolean = false,
        var originalImageBounds: CaptureRect? = null,
        var originalViewerWindowId: Int = -1,
        var originalViewerBounds: CaptureRect? = null,
        var viewerOpenDeadlineMillis: Long = 0L,
        var viewerStableSinceMillis: Long = 0L,
        var originalViewDeadlineMillis: Long = 0L,
        var originalViewStableSinceMillis: Long = 0L,
        var capturedMedia: MediaCandidate? = null,
        var historyCardHash: String? = null,
        var historySourceIdentity: String? = null,
        var historyStableSince: Long = 0L,
        var historyCardTitle: String? = null,
        var historyReportedTexts: List<String> = emptyList(),
        var voiceSourceIdentity: String? = null,
        var voiceSelection: VoiceTranscriptionPolicy.PinnedVoiceSelection? = null,
        var voiceSourceNode: AccessibilityNodeInfo? = null,
        var voiceWindowId: Int = -1,
        var voiceSourceStableSince: Long = 0L,
        var voiceTranscript: String? = null,
        var voiceTranscriptStableSince: Long = 0L,
        var voiceReportedTexts: List<String> = emptyList(),
        var voiceCopyAt: Long = 0L,
        var voiceWaitObservation: String? = null,
        var voiceRevealAt: Long = 0L,
        var unlockedForWorkflow: Boolean = false,
        @Volatile var result: AccessibilityReplyResult? = null,
    ) {
        val isImageCapture: Boolean get() = imageSender != null
        val isHistoryForward: Boolean get() = historySender != null
        val isVoiceTranscription: Boolean get() = voiceSender != null
    }

    private enum class ContactsPhase { Launching, ResettingTop, Reading, Encrypting }

    private data class ContactsView(
        val names: List<CharSequence>,
        val footerCount: Int?,
        val signature: Int,
    )

    private data class ContactScan(
        val profile: ContactsProfile,
        val deviceId: String,
        val startedAtMillis: Long,
        val assembly: ContactsPageAssembly = ContactsPageAssembly(),
        @Volatile var cancelled: Boolean = false,
        var phase: ContactsPhase = ContactsPhase.Launching,
        var resetSignature: Int? = null,
        var resetSignatureSinceMillis: Long = 0L,
        var stableSignature: Int? = null,
        var stableSignatureSinceMillis: Long = 0L,
        var topResetAttempts: Int = 0,
        var scrolls: Int = 0,
    )

    override fun onServiceConnected() {
        liveService = this
        ProbeRuntime.accessibilityConnected = true
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        val contacts = contactScan
        if (contacts != null) {
            if (contacts.phase != ContactsPhase.Launching && event?.eventType == AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED &&
                event.packageName?.toString() != NotificationSnapshot.WechatPackage
            ) {
                failContactsScan(contacts, "WINDOW_CHANGED")
            } else if (event?.packageName?.toString() == NotificationSnapshot.WechatPackage) {
                scheduleContactsTick(contacts)
            }
        }
        val active = session ?: return
        if (active.isVoiceTranscription && active.phase == Phase.VoiceWaitingTranscript &&
            event?.eventType == AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED &&
            VoiceTranscriptionPolicy.isFailureDialog(event.packageName?.toString(), event.className?.toString(), event.text.map(CharSequence::toString)) &&
            active.voiceIsCurrent?.invoke() == true
        ) {
            recordAccessibilityStage("VOICE_TRANSCRIPTION_FAILED")
            finishSession("FAILED", "VOICE_TRANSCRIPTION_FAILED")
            return
        }
        when (event?.packageName?.toString()) {
            SystemUiPackage -> if (active.phase == Phase.Unlocking) schedulePinTick(active)
            NotificationSnapshot.WechatPackage -> if (active.phase in setOf(
                    Phase.FindingSearch,
                    Phase.EnteringSearch,
                    Phase.SelectingResult,
                    Phase.OpeningWechat,
                    Phase.OpeningImageViewer,
                    Phase.WaitingOriginalView,
                    Phase.Verifying,
                    Phase.Capturing,
                    Phase.HistoryViewer, Phase.HistoryMenu, Phase.HistorySearch, Phase.HistoryResult, Phase.HistoryConfirm, Phase.HistoryVerify, Phase.HistoryReturnSource,
                    Phase.VoiceMenu, Phase.VoiceWaitingTranscript, Phase.VoiceCopyMenu,
                )) {
                processWechatWindow(active)
            }
        }
    }

    override fun onInterrupt() {
        contactScan?.let { failContactsScan(it, "ACCESSIBILITY_INTERRUPTED") }
        val active = session ?: return
        if (active.cancellation.cancel("ACCESSIBILITY_INTERRUPTED")) {
            finishSession("FAILED", "ACCESSIBILITY_INTERRUPTED")
        }
    }

    override fun onDestroy() {
        ProbeRuntime.accessibilityConnected = false
        if (liveService === this) liveService = null
        contactScan?.let { failContactsScan(it, "ACCESSIBILITY_DISCONNECTED") }
        finishSession("FAILED", "ACCESSIBILITY_DISCONNECTED")
        super.onDestroy()
    }

    private fun executeBlocking(
        contentIntent: PendingIntent?,
        expectedTitleHash: String,
        conversationTitle: String?,
        wechatUserId: Int,
        wechatUserSerial: Long,
        replyText: String?,
        imageSender: String? = null,
        imageIsCurrent: (() -> Boolean)? = null,
        historySender: String? = null,
        historyIsCurrent: (() -> Boolean)? = null,
        historyClaimSend: (() -> Boolean)? = null,
        historyCardsAfter: (() -> Int)? = null,
        historyCaptureTexts: ((List<String>) -> Unit)? = null,
        voiceSender: String? = null,
        voiceExpectedDuration: String? = null,
        voiceIsCurrent: (() -> Boolean)? = null,
        voiceVoicesAfter: (() -> Int)? = null,
        voiceCaptureTexts: ((List<String>) -> Unit)? = null,
    ): AccessibilityReplyResult {
        preemptContactsScan()
        val keyguard = getSystemService(KeyguardManager::class.java)
        val userManager = getSystemService(UserManager::class.java)
        val isImageCapture = imageSender != null
        if (!BuildConfig.LOCKSCREEN_ACCESSIBILITY_REPLY) return AccessibilityReplyResult("REMOTE_INPUT_UNSUPPORTED", "BUILD_FLAG_DISABLED")
        if (!LockscreenReplyPlatform.isEligible(Build.VERSION.SDK_INT)) return AccessibilityReplyResult("REMOTE_INPUT_UNSUPPORTED", "SDK_UNSUPPORTED")
        if (!userManager.isUserUnlocked) return AccessibilityReplyResult("FAILED", "USER_NOT_UNLOCKED_SINCE_BOOT")
        if (isImageCapture && serviceInfo.capabilities and AccessibilityServiceInfo.CAPABILITY_CAN_TAKE_SCREENSHOT == 0) {
            return AccessibilityReplyResult("FAILED", "SCREENSHOT_CAPABILITY_UNAVAILABLE")
        }
        if (expectedTitleHash.isBlank() ||
            (contentIntent != null && contentIntent.creatorPackage != NotificationSnapshot.WechatPackage) ||
            (contentIntent == null && (conversationTitle.isNullOrBlank() || !NotificationSnapshot.isAllowedWechatUserId(wechatUserId))) ||
            (isImageCapture && (contentIntent == null || imageSender.isNullOrEmpty() || imageIsCurrent == null || !NotificationSnapshot.isAllowedWechatUserId(wechatUserId))) ||
            (historySender != null && (conversationTitle.isNullOrBlank() ||
                historyIsCurrent == null || historyClaimSend == null || !NotificationSnapshot.isAllowedWechatUserId(wechatUserId))) ||
            (voiceSender != null && (voiceSender.isEmpty() || conversationTitle.isNullOrBlank() || voiceIsCurrent == null || voiceVoicesAfter == null ||
                !NotificationSnapshot.isAllowedWechatUserId(wechatUserId))) ||
            (!isImageCapture && historySender == null && voiceSender == null && replyText == null)
        ) {
            return AccessibilityReplyResult("WECHAT_ACTION_CHANGED", "TARGET_PRECHECK_FAILED")
        }

        val startedLocked = keyguard.isDeviceLocked
        val completion = CountDownLatch(1)
        var creationFailure: AccessibilityReplyResult? = null
        val createdSession: Session? = LockscreenPinStore.withAuthorizationLock {
            val automationEnabled = pinStore.automationEnabled()
            val fullyArmed = !startedLocked || pinStore.status(accessibilityLive = true, userUnlocked = true).armed
            if (!AccessibilityReplyPolicy.canStart(startedLocked, automationEnabled, fullyArmed)) {
                creationFailure = AccessibilityReplyResult("REMOTE_INPUT_UNSUPPORTED", "ACCESSIBILITY_NOT_ARMED")
                null
            } else {
                synchronized(this) sessionCreation@{
                    if (session != null) {
                        creationFailure = AccessibilityReplyResult("FAILED", "ACCESSIBILITY_BUSY")
                        return@sessionCreation null
                    }
                    val pin = if (AccessibilityReplyPolicy.requiresPin(startedLocked)) {
                        pinStore.beginAttempt() ?: run {
                            creationFailure = AccessibilityReplyResult("FAILED", "PIN_ATTEMPT_BLOCKED")
                            return@sessionCreation null
                        }
                    } else {
                        CharArray(0)
                    }
                    Session(
                        contentIntent = contentIntent,
                        expectedTitleHash = expectedTitleHash,
                        conversationTitle = conversationTitle.orEmpty().toCharArray(),
                        wechatUserId = wechatUserId,
                        wechatUserSerial = wechatUserSerial,
                        replyText = replyText?.toCharArray(),
                        imageSender = imageSender?.toCharArray(),
                        imageIsCurrent = imageIsCurrent,
                        historySender = historySender,
                        historyIsCurrent = historyIsCurrent,
                        historyClaimSend = historyClaimSend,
                        historyCardsAfter = historyCardsAfter,
                        historyCaptureTexts = historyCaptureTexts,
                        voiceSender = voiceSender,
                        voiceExpectedDuration = voiceExpectedDuration,
                        voiceIsCurrent = voiceIsCurrent,
                        voiceVoicesAfter = voiceVoicesAfter,
                        voiceCaptureTexts = voiceCaptureTexts,
                        pin = pin,
                        startedLocked = startedLocked,
                        completion = completion,
                    ).also { session = it }
                }
            }
        }
        val created = createdSession ?: return requireNotNull(creationFailure)
        handler.post {
            if (!created.cancellation.canAct()) {
                finishSession("FAILED", created.cancellation.stage() ?: "CANCELLED")
                return@post
            }
            try {
                if (!startedLocked) created.initialForegroundPackage = runCatching(::foregroundPackage).getOrNull()
                val started = created.cancellation.runIfActive {
                    startActivity(
                        Intent(this, UnlockGateActivity::class.java)
                            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP),
                    )
                    true
                } == true
                if (!started) {
                    finishSession("FAILED", created.cancellation.stage() ?: "CANCELLED")
                    return@post
                }
                if (startedLocked) {
                    handler.postDelayed({
                        if (session === created && created.phase == Phase.Unlocking) {
                            val timeoutStage = AccessibilityReplyPolicy.unlockTimeoutStage(created.gateDismissRequested, created.pinEntryStarted)
                            recordPinKeypadSnapshot(created, timeoutStage, timeout = true)
                            finishSession("FAILED", timeoutStage)
                        }
                    }, UnlockTimeoutMillis)
                }
            } catch (_: RuntimeException) {
                finishSession("FAILED", "GATE_START_FAILED")
            }
        }
        return awaitTerminalResult(created)
    }

    private fun awaitTerminalResult(active: Session): AccessibilityReplyResult {
        var interrupted = false
        val workflowDeadline = System.nanoTime() + TimeUnit.MILLISECONDS.toNanos(WorkflowTimeoutMillis)
        try {
            while (active.result == null) {
                val remaining = workflowDeadline - System.nanoTime()
                if (remaining <= 0L) {
                    cancelSession(active, workflowTimeoutStage(active))
                    break
                }
                try {
                    if (active.completion.await(remaining, TimeUnit.NANOSECONDS)) break
                } catch (_: InterruptedException) {
                    interrupted = true
                    cancelSession(active, "CALLER_INTERRUPTED")
                    break
                }
            }
            // Once cancellation is committed or send click reaches its point of no return,
            // only the shared main-thread finalizer may produce the wire-visible terminal.
            while (active.result == null) {
                try {
                    active.completion.await()
                } catch (_: InterruptedException) {
                    interrupted = true
                    cancelSession(active, "CALLER_INTERRUPTED")
                }
            }
            return requireNotNull(active.result)
        } finally {
            if (interrupted) Thread.currentThread().interrupt()
        }
    }

    private fun cancelSession(active: Session, stage: String): Boolean {
        if (!active.cancellation.cancel(stage)) return false
        handler.post {
            if (session === active) finishSession("FAILED", active.cancellation.stage() ?: stage)
        }
        return true
    }

    private fun workflowTimeoutStage(active: Session): String {
        if (active.isHistoryForward) return "HISTORY_TIMEOUT_${active.phase.name.uppercase()}"
        if (active.isVoiceTranscription) return "VOICE_TIMEOUT_${active.phase.name.uppercase()}"
        if (!active.isImageCapture) return "WORKFLOW_TIMEOUT"
        return when (active.phase) {
            Phase.Unlocking -> "WORKFLOW_TIMEOUT_UNLOCKING"
            Phase.OpeningWechat -> "WORKFLOW_TIMEOUT_OPENING_CHAT"
            Phase.OpeningImageViewer -> "WORKFLOW_TIMEOUT_OPENING_VIEWER"
            Phase.WaitingOriginalView -> "WORKFLOW_TIMEOUT_WAITING_ORIGINAL"
            Phase.Capturing -> "WORKFLOW_TIMEOUT_CAPTURING"
            Phase.Finishing -> "WORKFLOW_TIMEOUT_FINALIZING"
            else -> "WORKFLOW_TIMEOUT_${active.phase.name.uppercase()}"
        }
    }

    private fun clickNextPinDigit(active: Session) {
        if (session !== active || !active.cancellation.canAct() || active.phase != Phase.Unlocking || !active.gateDismissRequested || active.nextPinIndex >= active.pin.size) return
        if (!AccessibilityReplyPolicy.requiresPinEntry(getSystemService(KeyguardManager::class.java).isDeviceLocked)) {
            UnlockGateActivity.recheck()
            return
        }
        val candidates = rootsForPackage(SystemUiPackage)
            .flatMap(::walk)
            .filter { node ->
                node.isVisibleToUser && node.isEnabled && node.isClickable &&
                    LockscreenReplySelectors.isExactSystemPinNode(
                        node.packageName,
                        node.viewIdResourceName,
                        nodeLabels(node),
                        ancestorViewIds(node),
                        active.pin[active.nextPinIndex],
                    )
            }
        val index = LockscreenReplySelectors.uniqueCandidateIndex(candidates.map { true })
        val node = index?.let(candidates::get)
        if (node == null) {
            if (candidates.isEmpty() && !active.keypadWaitingLogged) {
                active.keypadWaitingLogged = true
                recordPinKeypadSnapshot(active, "PIN_KEYPAD_WAITING", timeout = false)
            }
            schedulePinTick(active)
            return
        }
        if (!active.pinEntryStarted) {
            active.pinEntryStarted = true
            recordAccessibilityStage("PIN_ENTRY_STARTED")
        }
        val clicked = active.cancellation.runIfActive {
            LockscreenReplySelectors.isExactSystemPinNode(
                node.packageName,
                node.viewIdResourceName,
                nodeLabels(node),
                ancestorViewIds(node),
                active.pin[active.nextPinIndex],
            ) &&
                node.performAction(AccessibilityNodeInfo.ACTION_CLICK)
        } == true
        if (!clicked) {
            finishSession("FAILED", "PIN_NODE_CLICK_FAILED")
            return
        }
        active.nextPinIndex += 1
        if (active.nextPinIndex == active.pin.size) {
            UnlockGateActivity.recheck()
        } else {
            schedulePinTick(active)
        }
    }

    private fun schedulePinTick(active: Session) {
        if (session !== active || !active.cancellation.canAct() || active.phase != Phase.Unlocking || !active.gateDismissRequested || active.nextPinIndex >= active.pin.size) return
        if (!handler.hasCallbacks(pinTick)) handler.postDelayed(pinTick, PinDigitDelayMillis)
    }

    private fun launchWechatFromGate(activity: Activity): Boolean {
        val active = session ?: return false
        if (!active.cancellation.canAct() || active.phase != Phase.Unlocking) return false
        if (AccessibilityReplyPolicy.requiresPinEntry(getSystemService(KeyguardManager::class.java).isDeviceLocked)) return false
        active.unlockedForWorkflow = true
        return try {
            val sent = active.cancellation.runIfActive {
                if (active.contentIntent != null) {
                    Api36PendingIntentLauncher.send(activity, active.contentIntent)
                } else {
                    launchWechatMain(active.wechatUserId, active.wechatUserSerial)
                }
                true
            } == true
            if (!sent) return false
            active.phase = if (active.contentIntent == null) Phase.FindingSearch else Phase.OpeningWechat
            if (active.isHistoryForward || active.isVoiceTranscription) handler.postDelayed(historyTick, UiSettleMillis)
            handler.postDelayed({
                if (session === active && active.phase !in setOf(
                        Phase.OpeningImageViewer,
                        Phase.WaitingOriginalView,
                        Phase.ReadyToClick,
                        Phase.Verifying,
                        Phase.Capturing,
                        Phase.Finishing,
                        Phase.HistoryViewer, Phase.HistoryMenu, Phase.HistorySearch, Phase.HistoryResult, Phase.HistoryConfirm, Phase.HistoryClaiming, Phase.HistoryVerify, Phase.HistoryReturnSource,
                        Phase.VoiceMenu, Phase.VoiceWaitingTranscript, Phase.VoiceLongPressing, Phase.VoiceCopyMenu, Phase.VoiceClipboardReading,
                    )) {
                    finishSession("FAILED", when {
                        active.isImageCapture -> "WECHAT_WINDOW_TIMEOUT_OPENING_CHAT"
                        active.isVoiceTranscription -> "VOICE_WINDOW_TIMEOUT_OPENING_CHAT"
                        else -> "WECHAT_WINDOW_TIMEOUT"
                    })
                }
            }, WechatWindowTimeoutMillis)
            true
        } catch (_: PendingIntent.CanceledException) {
            finishSession("PENDING_INTENT_CANCELED", "CONTENT_INTENT_CANCELED")
            true
        } catch (failure: RuntimeException) {
            recordAccessibilityDiagnostic("CONTENT_INTENT_SEND_FAILED", failure)
            finishSession("FAILED", "CONTENT_INTENT_SEND_FAILED")
            true
        }
    }

    private fun recordAccessibilityDiagnostic(stage: String, failure: RuntimeException) {
        try {
            ProbeStore(this).append(
                JSONObject()
                    .put("eventType", "syncDiagnostic")
                    .put("capturedAt", System.currentTimeMillis())
                    .put("stage", "ACCESSIBILITY_$stage")
                    .put("exceptionClass", failure.javaClass.name)
                    .toString(),
            )
        } catch (_: Exception) {
        }
    }

    private fun recordAccessibilityStage(stage: String) {
        try {
            ProbeStore(this).append(
                JSONObject()
                    .put("eventType", "syncDiagnostic")
                    .put("capturedAt", System.currentTimeMillis())
                    .put("stage", "ACCESSIBILITY_$stage")
                    .toString(),
            )
        } catch (_: Exception) {
        }
    }

    private fun recordPinKeypadSnapshot(active: Session, stage: String, timeout: Boolean) {
        if (timeout) {
            if (active.pinTimeoutSnapshotLogged) return
            active.pinTimeoutSnapshotLogged = true
        }
        try {
            val roots = rootsForPackage(SystemUiPackage)
            val keyNodes = roots.flatMap(::walk).filter(::hasExactSystemPinKeyId)
            ProbeStore(this).append(
                JSONObject()
                    .put("eventType", "syncDiagnostic")
                    .put("capturedAt", System.currentTimeMillis())
                    .put("stage", "ACCESSIBILITY_$stage")
                    .put("systemUiRootCount", roots.size)
                    .put("exactKeyNodeCount", keyNodes.size)
                    .put("visibleEnabledClickableKeyCount", keyNodes.count { it.isVisibleToUser && it.isEnabled && it.isClickable })
                    .put("sensitiveKeyCount", keyNodes.count { it.isAccessibilityDataSensitive })
                    .toString(),
            )
        } catch (_: Exception) {
        }
    }

    private fun launchWechatMain(wechatUserId: Int, wechatUserSerial: Long) {
        if (wechatUserId == 0) {
            val intent = packageManager.getLaunchIntentForPackage(NotificationSnapshot.WechatPackage)
                ?.takeIf { it.component?.packageName == NotificationSnapshot.WechatPackage }
                ?: throw IllegalStateException("WECHAT_LAUNCHER_UNAVAILABLE")
            startActivity(intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP))
            return
        }
        val launcherApps = getSystemService(LauncherApps::class.java)
        val user = getSystemService(UserManager::class.java).getUserForSerialNumber(wechatUserSerial)
            ?.takeIf { launcherApps.profiles.count { profile -> profile == it } == 1 }
            ?: throw IllegalStateException("WECHAT_PROFILE_UNAVAILABLE")
        val activities = launcherApps.getActivityList(NotificationSnapshot.WechatPackage, user)
        val activity = activities.singleOrNull { it.componentName.packageName == NotificationSnapshot.WechatPackage }
            ?: throw IllegalStateException("WECHAT_LAUNCHER_AMBIGUOUS")
        launcherApps.startMainActivity(activity.componentName, user, null, null)
    }

    private fun startContactsScan(profile: ContactsProfile): String {
        val keyguard = getSystemService(KeyguardManager::class.java)
        val users = getSystemService(UserManager::class.java)
        if (!users.isUserUnlocked) return "用户自开机后尚未解锁"
        if (keyguard.isDeviceLocked) return "请先手动解锁手机"
        val store = SyncStore.get(this)
        if (!store.paired()) return "请先完成配对"
        if (!ContactsProfileResolver.isAvailable(this, profile)) return "所选微信不可用"
        synchronized(this) {
            if (session != null || contactScan != null) return "当前 Accessibility 正忙"
            contactScan = ContactScan(profile, store.deviceId(), System.currentTimeMillis())
        }
        ProbeRuntime.contactsScanStatus = "正在打开${profile.label}通讯录"
        ProbeRuntime.contactsScanCount = 0
        ProbeRuntime.contactsPendingId = null
        handler.post {
            val active = contactScan ?: return@post
            try {
                // WeChat 8.0.77 on Xiaomi exposes the ids below. Stop on any selector drift.
                launchWechatMain(active.profile.wechatUserId, active.profile.userSerial)
                scheduleContactsTick(active)
            } catch (_: RuntimeException) {
                failContactsScan(active, "WECHAT_LAUNCH_FAILED")
            }
        }
        return "已开始扫描, 请勿切换界面"
    }

    private fun processContactsScan(active: ContactScan) {
        if (contactScan !== active || active.cancelled) return
        val now = System.currentTimeMillis()
        if (now - active.startedAtMillis > ContactsWorkflowTimeoutMillis) {
            failContactsScan(active, "SCAN_TIMEOUT")
            return
        }
        if (foregroundPackage() != NotificationSnapshot.WechatPackage) {
            if (active.phase == ContactsPhase.Launching && now - active.startedAtMillis < ContactsLaunchTimeoutMillis) scheduleContactsTick(active)
            else failContactsScan(active, "WINDOW_CHANGED")
            return
        }
        val roots = rootsForPackage(NotificationSnapshot.WechatPackage)
        if (roots.size != 1) {
            if (active.phase == ContactsPhase.Launching && now - active.startedAtMillis < ContactsLaunchTimeoutMillis) scheduleContactsTick(active)
            else failContactsScan(active, "WECHAT_WINDOW_AMBIGUOUS")
            return
        }
        val nodes = walk(roots.single())
        val contactsTabs = nodes.filter { it.isVisibleToUser && it.viewIdResourceName == ContactsTabViewId && it.text?.toString()?.trim() == ContactsTitle }
        if (contactsTabs.size != 1) {
            if (active.phase == ContactsPhase.Launching && now - active.startedAtMillis < ContactsLaunchTimeoutMillis) scheduleContactsTick(active)
            else failContactsScan(active, "CONTACTS_TAB_CHANGED")
            return
        }
        val tab = contactsTabs.single()
        if (!tab.isSelected) {
            if (active.phase != ContactsPhase.Launching) {
                failContactsScan(active, "CONTACTS_TAB_LEFT")
                return
            }
            val target = clickableAncestor(tab) ?: tab
            if (!target.performAction(AccessibilityNodeInfo.ACTION_CLICK)) {
                failContactsScan(active, "CONTACTS_TAB_CLICK_FAILED")
                return
            }
            scheduleContactsTick(active)
            return
        }
        val contactsTitle = nodes.filter { it.isVisibleToUser && it.viewIdResourceName == AndroidTitleViewId && it.text?.toString()?.trim() == ContactsTitle }
        if (contactsTitle.size != 1) {
            failContactsScan(active, "CONTACTS_SURFACE_CHANGED")
            return
        }
        val recycler = nodes.singleOrNull { it.isVisibleToUser && it.viewIdResourceName == ContactsRecyclerViewId }
        if (recycler == null) {
            failContactsScan(active, "CONTACTS_LIST_CHANGED")
            return
        }
        val view = contactsView(nodes) ?: run {
            failContactsScan(active, "CONTACTS_ROWS_CHANGED")
            return
        }
        when (active.phase) {
            ContactsPhase.Launching -> {
                active.phase = ContactsPhase.ResettingTop
                active.resetSignature = null
                active.resetSignatureSinceMillis = 0L
                scheduleContactsTick(active)
            }
            ContactsPhase.ResettingTop -> {
                if (active.resetSignature != view.signature) {
                    active.resetSignature = view.signature
                    active.resetSignatureSinceMillis = now
                }
                if (isContactsTop(nodes) && ContactsScanPolicy.stableFor(active.resetSignature, active.resetSignatureSinceMillis, view.signature, now, ContactsStableMillis)) {
                    active.assembly.confirmTop()
                    active.phase = ContactsPhase.Reading
                    active.stableSignature = null
                    active.stableSignatureSinceMillis = 0L
                    scheduleContactsTick(active)
                } else {
                    active.topResetAttempts += 1
                    if (active.topResetAttempts > MaxContactsTopResetAttempts) {
                        failContactsScan(active, "CONTACTS_TOP_PROOF_MISSING")
                    } else {
                        recycler.performAction(AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD)
                        scheduleContactsTick(active)
                    }
                }
            }
            ContactsPhase.Reading -> {
                if (active.stableSignature != view.signature) {
                    active.stableSignature = view.signature
                    active.stableSignatureSinceMillis = now
                }
                if (!ContactsScanPolicy.stableFor(active.stableSignature, active.stableSignatureSinceMillis, view.signature, now, ContactsStableMillis)) {
                    scheduleContactsTick(active)
                    return
                }
                try {
                    active.assembly.addStablePage(view.names)
                    view.footerCount?.let(active.assembly::observeFooter)
                    ProbeRuntime.contactsScanCount = active.assembly.count()
                } catch (_: IllegalArgumentException) {
                    failContactsScan(active, "CONTACTS_LIMIT_EXCEEDED")
                    return
                }
                if (view.footerCount != null) {
                    val complete = runCatching(active.assembly::complete).getOrNull()
                    if (complete == null) {
                        failContactsScan(active, "CONTACTS_COUNT_MISMATCH")
                    } else encryptContactsSnapshot(active, complete)
                    return
                }
                active.scrolls += 1
                if (active.scrolls > MaxContactsScrolls) {
                    failContactsScan(active, "CONTACTS_SCROLL_BOUND_EXCEEDED")
                    return
                }
                if (!recycler.isScrollable || !recycler.performAction(AccessibilityNodeInfo.ACTION_SCROLL_FORWARD)) {
                    failContactsScan(active, "CONTACTS_BOTTOM_PROOF_MISSING")
                    return
                }
                active.stableSignature = null
                active.stableSignatureSinceMillis = 0L
                scheduleContactsTick(active)
            }
            ContactsPhase.Encrypting -> Unit
        }
    }

    private fun contactsView(nodes: List<AccessibilityNodeInfo>): ContactsView? {
        val names = nodes.filter { node ->
            node.isVisibleToUser && node.viewIdResourceName == ContactsNameViewId &&
                ancestorViewIds(node).contains(ContactsTableViewId) && !node.text.isNullOrBlank()
        }.map { requireNotNull(it.text) }
        val footerNodes = nodes.filter { it.isVisibleToUser && it.viewIdResourceName == ContactsFooterViewId }
        val footerCount = when (footerNodes.size) {
            0 -> null
            1 -> ContactsFooter.matchEntire(footerNodes.single().text?.toString()?.trim().orEmpty())?.groupValues?.get(1)?.toIntOrNull()
                ?: return null
            else -> return null
        }
        val signature = 31 * names.map { it.toString() }.hashCode() + (footerCount ?: -1)
        return ContactsView(names, footerCount, signature)
    }

    private fun isContactsTop(nodes: List<AccessibilityNodeInfo>): Boolean = nodes.any {
        it.isVisibleToUser && it.viewIdResourceName == ContactsNewFriendViewId && it.text?.toString()?.trim() == ContactsNewFriendTitle
    }

    private fun encryptContactsSnapshot(active: ContactScan, names: List<String>) {
        active.phase = ContactsPhase.Encrypting
        ProbeRuntime.contactsScanStatus = "正在加密${active.assembly.count()}位联系人"
        ContactsExecutor.execute {
            try {
                val store = SyncStore.get(this)
                if (active.cancelled || contactScan !== active || !store.isCurrentDevice(active.deviceId)) return@execute
                val key = store.messageEncryptionContext(active.deviceId).a2iKey
                val capturedAt = System.currentTimeMillis()
                val id = SyncProtocol.uuidV7(capturedAt)
                val envelope = try { SyncProtocol.encryptContacts(key, id, active.deviceId, capturedAt, active.profile.wechatUserId, names) }
                finally { key.fill(0) }
                synchronized(this@LockscreenAccessibilityReplyService) {
                    if (active.cancelled || contactScan !== active || !store.isCurrentDevice(active.deviceId)) return@execute
                    ContactsPendingStore.get(this).save(ContactsPending(id, active.deviceId, active.profile.wechatUserId, capturedAt, envelope))
                    ProbeRuntime.contactsPendingId = id
                }
                handler.post {
                    if (contactScan === active && !active.cancelled && store.isCurrentDevice(active.deviceId) && ProbeRuntime.contactsPendingId == id) {
                        ContactsSyncNetwork.enqueue(this)
                        finishContactsScan(active, "已加密入队, 正在同步", capturedAt)
                    }
                }
            } catch (_: Exception) {
                handler.post { failContactsScan(active, "CONTACTS_ENCRYPT_OR_SAVE_FAILED") }
            }
        }
    }

    private fun scheduleContactsTick(active: ContactScan) {
        if (contactScan === active && !active.cancelled) {
            handler.removeCallbacks(contactsTick)
            handler.postDelayed(contactsTick, ContactsPollMillis)
        }
    }

    private fun preemptContactsScan() {
        contactScan?.let { cancelContactsScan(it, "PREEMPTED_BY_INCOMING") }
    }

    private fun cancelContactsScan(active: ContactScan, stage: String) {
        synchronized(this) {
            if (contactScan !== active) return
            active.cancelled = true
            contactScan = null
            handler.removeCallbacks(contactsTick)
            ProbeRuntime.contactsScanStatus = "扫描已取消: $stage"
        }
    }

    private fun failContactsScan(active: ContactScan, stage: String) {
        synchronized(this) {
            if (contactScan !== active) return
            active.cancelled = true
            contactScan = null
            handler.removeCallbacks(contactsTick)
            ProbeRuntime.contactsScanStatus = "扫描失败: $stage"
        }
        recordAccessibilityStage("${stage}_SCANNED_${active.assembly.count()}_EXPECTED_${active.assembly.expectedCount ?: -1}")
        if (foregroundPackage() == NotificationSnapshot.WechatPackage) {
            startActivity(Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP))
        }
    }

    private fun finishContactsScan(active: ContactScan, status: String, capturedAt: Long) {
        synchronized(this) {
            if (contactScan !== active) return
            contactScan = null
            handler.removeCallbacks(contactsTick)
            ProbeRuntime.contactsScanStatus = status
            ProbeRuntime.contactsScanCapturedAt = capturedAt
        }
        recordAccessibilityStage("CONTACTS_SCAN_QUEUED_COUNT_${active.assembly.count()}")
        // Return only while this manual scan still owns WeChat's foreground window.
        if (foregroundPackage() == NotificationSnapshot.WechatPackage) {
            startActivity(Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP))
        }
    }

    private fun processWechatWindow(active: Session) {
        if (session !== active || !active.cancellation.canAct()) return
        if (active.isHistoryForward) {
            if (active.phase in setOf(Phase.FindingSearch, Phase.EnteringSearch, Phase.SelectingResult)) {
                processConversationSearch(active, rootsForPackage(NotificationSnapshot.WechatPackage))
            } else processHistoryForward(active)
            return
        }
        if (active.isVoiceTranscription) {
            if (active.phase in setOf(Phase.FindingSearch, Phase.EnteringSearch, Phase.SelectingResult)) {
                processConversationSearch(active, rootsForPackage(NotificationSnapshot.WechatPackage))
            } else processVoiceTranscription(active)
            return
        }
        val roots = rootsForPackage(NotificationSnapshot.WechatPackage)
        if (active.phase in setOf(Phase.FindingSearch, Phase.EnteringSearch, Phase.SelectingResult)) {
            processConversationSearch(active, roots)
            return
        }
        // ponytail: User-authorized experiment skips title verification; restore it when accessible identity is available.
        if (roots.isEmpty()) {
            if (active.phase == Phase.OpeningWechat) return
            if (active.isImageCapture && active.phase == Phase.OpeningImageViewer) {
                active.viewerStableSinceMillis = 0L
                if (WechatImageViewerPolicy.withinDeadline(SystemClock.uptimeMillis(), active.viewerOpenDeadlineMillis)) {
                    scheduleViewerUiPoll(active, Phase.OpeningImageViewer)
                } else finishSession("FAILED", "IMAGE_VIEWER_TIMEOUT")
                return
            }
            if (active.isImageCapture && active.phase == Phase.WaitingOriginalView) {
                active.originalViewStableSinceMillis = 0L
                if (WechatImageViewerPolicy.withinDeadline(SystemClock.uptimeMillis(), active.originalViewDeadlineMillis)) {
                    scheduleViewerUiPoll(active, Phase.WaitingOriginalView)
                } else finishSession("FAILED", "ORIGINAL_VIEW_TIMEOUT")
                return
            }
            finishSession("WECHAT_ACTION_CHANGED", "WECHAT_WINDOW_LOST")
            return
        }
        if (active.isImageCapture) {
            processImageCapture(active)
            return
        }
        val replyText = requireNotNull(active.replyText)
        val nodes = roots.flatMap(::walk)
        val inputs = nodes.filter(::isBottomComposerInput)
        val input = LockscreenReplySelectors.uniqueCandidateIndex(inputs.map { true })?.let(inputs::get)
        if (input == null) {
            if (active.phase == Phase.OpeningWechat) return
            finishSession("WECHAT_ACTION_CHANGED", "AMBIGUOUS_REPLY_CONTROLS")
            return
        }

        if (active.phase == Phase.Verifying) {
            val current = input.text?.toString().orEmpty()
            if (current.isEmpty()) {
                finishSession("SENT_TO_WECHAT", "SEND_ACCEPTED")
            } else if (current != replyText.concatToString()) {
                finishSession("FAILED", "SEND_ACCEPTANCE_AMBIGUOUS")
            } else if (System.currentTimeMillis() - active.clickAt >= AcceptanceTimeoutMillis) {
                finishSession("FAILED", "SEND_ACCEPTANCE_TIMEOUT")
            } else {
                handler.postDelayed({ processWechatWindow(active) }, AcceptancePollMillis)
            }
            return
        }

        when (active.phase) {
            Phase.OpeningWechat -> {
                val arguments = Bundle().apply {
                    putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, replyText.concatToString())
                }
                val textSet = active.cancellation.runIfActive {
                    input.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, arguments)
                } == true
                if (!textSet) {
                    finishSession("FAILED", "SET_TEXT_FAILED")
                    return
                }
                active.inputObservationDeadlineMillis = SystemClock.uptimeMillis() + InputTextObservationTimeoutMillis
                if (!active.setTextAcceptedLogged) {
                    active.setTextAcceptedLogged = true
                    recordAccessibilityStage("SET_TEXT_ACCEPTED")
                }
                active.phase = Phase.ReadyToClick
                handler.postDelayed({ processWechatWindow(active) }, UiSettleMillis)
            }
            Phase.ReadyToClick -> {
                val inputMatches = input.refresh() && input.text?.toString() == replyText.concatToString()
                when (AccessibilityReplyPolicy.inputTextObservation(inputMatches, SystemClock.uptimeMillis(), active.inputObservationDeadlineMillis)) {
                    InputTextObservation.Wait -> {
                        handler.postDelayed({ processWechatWindow(active) }, InputTextObservationPollMillis)
                        return
                    }
                    InputTextObservation.Fail -> {
                        if (!active.inputTextObservationTimeoutLogged) {
                            active.inputTextObservationTimeoutLogged = true
                            recordAccessibilityStage("INPUT_TEXT_OBSERVATION_TIMEOUT")
                        }
                        finishSession("WECHAT_ACTION_CHANGED", "INPUT_RECHECK_FAILED")
                        return
                    }
                    InputTextObservation.Send -> if (!active.inputTextObservedLogged) {
                        active.inputTextObservedLogged = true
                        recordAccessibilityStage("INPUT_TEXT_OBSERVED")
                    }
                }
                val sends = nodes.filter { node ->
                    node.isVisibleToUser && node.isEnabled && node.isClickable &&
                        nodeLabels(node).any { LockscreenReplySelectors.normalizeTitle(it) in SendLabels } &&
                        isSameRowRightSend(input, node)
                }
                val send = LockscreenReplySelectors.uniqueCandidateIndex(sends.map { true })?.let(sends::get)
                if (!LockscreenReplySelectors.hasUniqueReadySendControl(sends.size) || send == null) {
                    finishSession("WECHAT_ACTION_CHANGED", "AMBIGUOUS_REPLY_CONTROLS")
                    return
                }
                val clicked = active.cancellation.commitIfActive {
                    send.performAction(AccessibilityNodeInfo.ACTION_CLICK)
                }
                if (!clicked) {
                    finishSession("FAILED", "SEND_CLICK_FAILED")
                    return
                }
                active.clickAt = System.currentTimeMillis()
                active.phase = Phase.Verifying
                handler.postDelayed({ processWechatWindow(active) }, AcceptancePollMillis)
            }
            Phase.Verifying -> Unit
            else -> Unit
        }
    }

    private fun processImageCapture(active: Session) {
        when (active.phase) {
            Phase.OpeningImageViewer -> return processImageViewer(active)
            Phase.WaitingOriginalView -> return processOriginalViewWait(active)
            Phase.OpeningWechat -> Unit
            else -> return
        }
        val current = currentImageSource(active) ?: return
        if (active.stableImageSource != current) {
            active.stableImageSource = current
            active.imageStableSinceMillis = SystemClock.uptimeMillis()
            handler.postDelayed({
                if (session === active && active.phase == Phase.OpeningWechat) processWechatWindow(active)
            }, ImageStabilityPollMillis)
            return
        }
        if (SystemClock.uptimeMillis() - active.imageStableSinceMillis < ImageStabilityMillis) {
            handler.postDelayed({
                if (session === active && active.phase == Phase.OpeningWechat) processWechatWindow(active)
            }, ImageStabilityPollMillis)
            return
        }
        if (!imageRequestIsCurrent(active) || currentImageSource(active) != current) {
            finishSession("WECHAT_ACTION_CHANGED", "IMAGE_TARGET_CHANGED")
            return
        }
        openImageViewer(active, current)
    }

    private fun openImageViewer(active: Session, expected: ImageSourceSelection) {
        if (!imageRequestIsCurrent(active) || currentImageSource(active) != expected) {
            finishSession("WECHAT_ACTION_CHANGED", "IMAGE_TARGET_CHANGED")
            return
        }
        val image = currentImageNode(expected)
        if (image == null || active.cancellation.runIfActive {
                image.performAction(AccessibilityNodeInfo.ACTION_CLICK)
            } != true
        ) {
            finishSession("FAILED", "IMAGE_VIEWER_OPEN_FAILED")
            return
        }
        active.originalImageBounds = expected.selection.imageBounds
        active.viewerOpenDeadlineMillis = SystemClock.uptimeMillis() + ViewerOpenTimeoutMillis
        active.viewerStableSinceMillis = 0L
        active.phase = Phase.OpeningImageViewer
        scheduleViewerUiPoll(active, Phase.OpeningImageViewer)
    }

    private fun processImageViewer(active: Session) {
        if (!imageRequestIsCurrent(active)) {
            finishSession("WECHAT_ACTION_CHANGED", "IMAGE_TARGET_CHANGED")
            return
        }
        val viewer = captureImageViewer(active) ?: run {
            active.viewerStableSinceMillis = 0L
            if (WechatImageViewerPolicy.withinDeadline(SystemClock.uptimeMillis(), active.viewerOpenDeadlineMillis)) {
                scheduleViewerUiPoll(active, Phase.OpeningImageViewer)
            } else finishSession("FAILED", "IMAGE_VIEWER_TIMEOUT")
            return
        }
        val originalControls = currentViewOriginalNodes()
        if (originalControls.size > 1) {
            finishSession("WECHAT_ACTION_CHANGED", "ORIGINAL_CONTROL_AMBIGUOUS")
            return
        }
        val originalControl = originalControls.singleOrNull()
        if (originalControl != null) {
            if (active.cancellation.runIfActive { originalControl.performAction(AccessibilityNodeInfo.ACTION_CLICK) } != true) {
                finishSession("FAILED", "ORIGINAL_CONTROL_CLICK_FAILED")
                return
            }
            active.originalViewDeadlineMillis = SystemClock.uptimeMillis() + OriginalViewTimeoutMillis
            active.originalViewStableSinceMillis = 0L
            active.phase = Phase.WaitingOriginalView
            scheduleViewerUiPoll(active, Phase.WaitingOriginalView)
            return
        }
        val now = SystemClock.uptimeMillis()
        if (currentViewerLoadingIndicatorCount() > 0) {
            active.viewerStableSinceMillis = 0L
            if (now >= active.viewerOpenDeadlineMillis) finishSession("FAILED", "IMAGE_VIEWER_TIMEOUT")
            else scheduleViewerUiPoll(active, Phase.OpeningImageViewer)
            return
        }
        if (active.viewerStableSinceMillis == 0L) active.viewerStableSinceMillis = now
        if (!WechatImageViewerPolicy.hasSettled(now, active.viewerStableSinceMillis, ViewerSettleMillis)) {
            scheduleViewerUiPoll(active, Phase.OpeningImageViewer)
            return
        }
        startViewerScreenshot(active, viewer)
    }

    private fun processOriginalViewWait(active: Session) {
        if (!imageRequestIsCurrent(active)) {
            finishSession("WECHAT_ACTION_CHANGED", "IMAGE_TARGET_CHANGED")
            return
        }
        val viewer = retainedImageViewer(active) ?: run {
            active.originalViewStableSinceMillis = 0L
            if (SystemClock.uptimeMillis() >= active.originalViewDeadlineMillis) finishSession("FAILED", "ORIGINAL_VIEW_TIMEOUT")
            else scheduleViewerUiPoll(active, Phase.WaitingOriginalView)
            return
        }
        val controls = currentViewOriginalNodes()
        if (controls.size > 1) {
            finishSession("WECHAT_ACTION_CHANGED", "ORIGINAL_CONTROL_AMBIGUOUS")
            return
        }
        val now = SystemClock.uptimeMillis()
        if (controls.isNotEmpty() || currentViewerLoadingIndicatorCount() > 0) {
            active.originalViewStableSinceMillis = 0L
            if (now >= active.originalViewDeadlineMillis) finishSession("FAILED", "ORIGINAL_VIEW_TIMEOUT")
            else scheduleViewerUiPoll(active, Phase.WaitingOriginalView)
            return
        }
        if (active.originalViewStableSinceMillis == 0L) active.originalViewStableSinceMillis = now
        if (now - active.originalViewStableSinceMillis < OriginalViewSettleMillis) {
            scheduleViewerUiPoll(active, Phase.WaitingOriginalView)
            return
        }
        startViewerScreenshot(active, viewer)
    }

    private fun startViewerScreenshot(active: Session, viewer: ViewerCaptureSelection) {
        if (active.screenshotStarted || !imageRequestIsCurrent(active) || retainedImageViewer(active) != viewer) {
            finishSession("WECHAT_ACTION_CHANGED", "IMAGE_VIEWER_CHANGED")
            return
        }
        active.screenshotStarted = true
        active.phase = Phase.Capturing
        try {
            takeScreenshotOfWindow(viewer.windowId, mainExecutor, object : TakeScreenshotCallback {
                override fun onSuccess(screenshot: ScreenshotResult) {
                    handleViewerScreenshot(active, viewer, screenshot)
                }

                override fun onFailure(errorCode: Int) {
                    if (session === active && active.phase == Phase.Capturing) finishSession("FAILED", "SCREENSHOT_FAILED")
                }
            })
        } catch (_: RuntimeException) {
            finishSession("FAILED", "SCREENSHOT_FAILED")
        }
    }

    private fun scheduleViewerUiPoll(active: Session, expectedPhase: Phase) {
        handler.postDelayed({
            if (session === active && active.phase == expectedPhase) processWechatWindow(active)
        }, OriginalUiPollMillis)
    }

    private fun handleViewerScreenshot(active: Session, expected: ViewerCaptureSelection, screenshot: ScreenshotResult) {
        val buffer = screenshot.hardwareBuffer
        if (session !== active || active.phase != Phase.Capturing || !active.cancellation.canAct()) {
            buffer.close()
            return
        }
        if (!imageRequestIsCurrent(active) || retainedImageViewer(active) != expected) {
            buffer.close()
            finishSession("WECHAT_ACTION_CHANGED", "IMAGE_VIEWER_CHANGED")
            return
        }
        try {
            ImageExecutor.execute {
                val result = processViewerScreenshot(expected, buffer, screenshot.colorSpace)
                if (!handler.post { finalizeViewerScreenshot(active, expected, result) }) result.media?.bytes?.fill(0)
            }
        } catch (_: RuntimeException) {
            buffer.close()
            finishSession("FAILED", "SCREENSHOT_PROCESSING_FAILED")
        }
    }

    private fun processViewerScreenshot(
        expected: ViewerCaptureSelection,
        buffer: android.hardware.HardwareBuffer,
        colorSpace: android.graphics.ColorSpace,
    ): ImageProcessingResult {
        var hardware: Bitmap? = null
        var source: Bitmap? = null
        var crop: Bitmap? = null
        var candidate: MediaCandidate? = null
        return try {
            hardware = Bitmap.wrapHardwareBuffer(buffer, colorSpace)
                ?: return ImageProcessingResult(failureStage = "SCREENSHOT_DECODE_FAILED")
            source = hardware.copy(Bitmap.Config.ARGB_8888, false)
                ?: return ImageProcessingResult(failureStage = "SCREENSHOT_DECODE_FAILED")
            val cropBounds = WechatImageViewerPolicy.mapAspectFitCrop(
                expected.expectedImageBounds,
                expected.viewerBounds,
                expected.rootBounds,
                source.width,
                source.height,
            ) ?: return ImageProcessingResult(failureStage = "VIEWER_CROP_UNSAFE")
            crop = Bitmap.createBitmap(cropBounds.width, cropBounds.height, Bitmap.Config.ARGB_8888)
            Canvas(crop).drawBitmap(
                source,
                Rect(cropBounds.left, cropBounds.top, cropBounds.right, cropBounds.bottom),
                Rect(0, 0, crop.width, crop.height),
                Paint(Paint.ANTI_ALIAS_FLAG or Paint.FILTER_BITMAP_FLAG),
            )
            candidate = NotificationMedia.capturedViewerImage(crop)
                ?: return ImageProcessingResult(failureStage = "IMAGE_ENCODING_FAILED")
            ImageProcessingResult(media = candidate).also { candidate = null }
        } catch (_: RuntimeException) {
            ImageProcessingResult(failureStage = "SCREENSHOT_PROCESSING_FAILED")
        } finally {
            candidate?.bytes?.fill(0)
            crop?.recycle()
            source?.recycle()
            hardware?.recycle()
            buffer.close()
        }
    }

    private fun finalizeViewerScreenshot(active: Session, expected: ViewerCaptureSelection, result: ImageProcessingResult) {
        val candidate = result.media
        if (session !== active || active.phase != Phase.Capturing || !active.cancellation.canAct()) {
            candidate?.bytes?.fill(0)
            return
        }
        if (result.failureStage != null || candidate == null) {
            candidate?.bytes?.fill(0)
            finishSession("FAILED", result.failureStage ?: "SCREENSHOT_PROCESSING_FAILED")
            return
        }
        if (!imageRequestIsCurrent(active) || retainedImageViewer(active) != expected) {
            candidate.bytes.fill(0)
            finishSession("WECHAT_ACTION_CHANGED", "IMAGE_VIEWER_CHANGED")
            return
        }
        active.capturedMedia = candidate
        finishSession("IMAGE_CAPTURED", "VIEWER_IMAGE_CAPTURED")
    }

    private fun imageRequestIsCurrent(active: Session): Boolean =
        active.imageIsCurrent?.let { runCatching(it).getOrDefault(false) } == true

    private fun currentImageSource(active: Session): ImageSourceSelection? {
        val root = rootInActiveWindow?.takeIf { it.packageName?.toString() == NotificationSnapshot.WechatPackage } ?: return null
        val windowId = root.windowId
        val matchingWindows = windows.filter { window ->
            window.id == windowId && window.isActive && window.root?.packageName?.toString() == NotificationSnapshot.WechatPackage
        }
        if (matchingWindows.size != 1) return null
        val rootBounds = Rect().also(root::getBoundsInScreen).toCaptureRect()
        val nodes = walk(root)
        val composers = nodes.filter(::isBottomComposerInput)
        val composer = composers.singleOrNull() ?: return null
        val composerBounds = Rect().also(composer::getBoundsInScreen).toCaptureRect()
        val selection = ImageCapturePolicy.selectLatest(
            rows = nodes.filter { it.viewIdResourceName == ImageCapturePolicy.RowViewId }.map(::imageRowSnapshot),
            sender = active.imageSender?.concatToString() ?: return null,
            rootBounds = rootBounds,
            chatTop = maxOf(rootBounds.top, WechatChatTopPx),
            chatBottom = composerBounds.top,
        ) ?: return null
        return ImageSourceSelection(windowId, rootBounds, selection)
    }

    private fun currentImageNode(expected: ImageSourceSelection): AccessibilityNodeInfo? {
        val root = rootInActiveWindow?.takeIf {
            it.packageName?.toString() == NotificationSnapshot.WechatPackage && it.windowId == expected.windowId
        } ?: return null
        val candidates = walk(root).filter { node ->
            node.isVisibleToUser && node.isEnabled && node.isClickable &&
                node.viewIdResourceName == ImageCapturePolicy.ImageViewId &&
                node.className?.toString() == ImageCapturePolicy.ImageClass &&
                node.contentDescription?.toString() == ImageCapturePolicy.ImageDescription &&
                Rect().also(node::getBoundsInScreen).toCaptureRect() == expected.selection.imageBounds &&
                messageRowAncestor(node)?.let { row ->
                    Rect().also(row::getBoundsInScreen).toCaptureRect() == expected.selection.rowBounds &&
                        hasExpectedImageAncestry(node, row)
                } == true
        }
        return candidates.singleOrNull()
    }

    private fun messageRowAncestor(node: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        var parent = node.parent
        while (parent != null) {
            if (parent.viewIdResourceName == ImageCapturePolicy.RowViewId) return parent
            parent = parent.parent
        }
        return null
    }

    private fun captureImageViewer(active: Session): ViewerCaptureSelection? {
        val root = rootInActiveWindow?.takeIf { it.packageName?.toString() == NotificationSnapshot.WechatPackage } ?: return null
        val rootBounds = Rect().also(root::getBoundsInScreen).toCaptureRect()
        val snapshots = walk(root).map(::originalNodeSnapshot)
        val bounds = WechatImageViewerPolicy.selectViewer(snapshots, rootBounds)?.bounds ?: return null
        val expectedImageBounds = active.originalImageBounds ?: return null
        active.originalViewerWindowId = root.windowId
        active.originalViewerBounds = bounds
        return ViewerCaptureSelection(root.windowId, rootBounds, bounds, expectedImageBounds)
    }

    private fun retainedImageViewer(active: Session): ViewerCaptureSelection? {
        if (rootInActiveWindow?.packageName?.toString() != NotificationSnapshot.WechatPackage) return null
        val expectedWindowId = active.originalViewerWindowId.takeIf { it >= 0 } ?: return null
        val expectedBounds = active.originalViewerBounds ?: return null
        val expectedImageBounds = active.originalImageBounds ?: return null
        val roots = windows.mapNotNull { it.root }.filter {
            it.packageName?.toString() == NotificationSnapshot.WechatPackage && it.windowId == expectedWindowId
        }
        val matches = roots.mapNotNull { root ->
            val rootBounds = Rect().also(root::getBoundsInScreen).toCaptureRect()
            WechatImageViewerPolicy.selectViewer(walk(root).map(::originalNodeSnapshot), rootBounds)?.let { it to rootBounds }
        }
        val (matched, rootBounds) = matches.singleOrNull() ?: return null
        if (matched.bounds != expectedBounds) return null
        return ViewerCaptureSelection(expectedWindowId, rootBounds, expectedBounds, expectedImageBounds)
    }

    private fun currentViewOriginalNodes(): List<AccessibilityNodeInfo> {
        val root = rootInActiveWindow?.takeIf { it.packageName?.toString() == NotificationSnapshot.WechatPackage } ?: return emptyList()
        val rootBounds = Rect().also(root::getBoundsInScreen).toCaptureRect()
        val nodes = walk(root)
        return nodes.asSequence()
            .filter { node -> WechatImageViewerPolicy.viewOriginalControls(listOf(originalNodeSnapshot(node)), rootBounds).size == 1 }
            .mapNotNull(::clickableAncestor)
            .distinctBy(::nodeIdentity)
            .toList()
    }

    private fun currentViewerLoadingIndicatorCount(): Int {
        val root = rootInActiveWindow?.takeIf { it.packageName?.toString() == NotificationSnapshot.WechatPackage } ?: return 0
        return walk(root).count { node ->
            node.isVisibleToUser && node.className?.toString() == "android.widget.ProgressBar"
        }
    }

    private fun originalNodeSnapshot(node: AccessibilityNodeInfo): OriginalImageNodeSnapshot =
        OriginalImageNodeSnapshot(
            viewId = node.viewIdResourceName,
            className = node.className?.toString(),
            label = node.text?.toString()?.takeIf(String::isNotBlank) ?: node.contentDescription?.toString(),
            visible = node.isVisibleToUser,
            enabled = node.isEnabled,
            bounds = Rect().also(node::getBoundsInScreen).toCaptureRect(),
        )

    private fun imageRowSnapshot(row: AccessibilityNodeInfo): ImageCaptureRowSnapshot {
        val descendants = walk(row)
        return ImageCaptureRowSnapshot(
            viewId = row.viewIdResourceName,
            className = row.className?.toString(),
            visible = row.isVisibleToUser,
            bounds = Rect().also(row::getBoundsInScreen).toCaptureRect(),
            containsText = descendants.any { it !== row && !it.text?.toString().isNullOrBlank() },
            images = descendants.filter { it.viewIdResourceName == ImageCapturePolicy.ImageViewId }.map { node ->
                imageNodeSnapshot(node, expectedAncestry = hasExpectedImageAncestry(node, row))
            },
            avatars = descendants.filter { it.viewIdResourceName == ImageCapturePolicy.AvatarViewId }.map { node ->
                imageNodeSnapshot(node, expectedAncestry = true)
            },
        )
    }

    private fun imageNodeSnapshot(node: AccessibilityNodeInfo, expectedAncestry: Boolean): ImageCaptureNodeSnapshot =
        ImageCaptureNodeSnapshot(
            viewId = node.viewIdResourceName,
            className = node.className?.toString(),
            contentDescription = node.contentDescription?.toString(),
            visible = node.isVisibleToUser,
            clickable = node.isClickable,
            bounds = Rect().also(node::getBoundsInScreen).toCaptureRect(),
            expectedAncestry = expectedAncestry,
        )

    private fun hasExpectedImageAncestry(node: AccessibilityNodeInfo, row: AccessibilityNodeInfo): Boolean {
        val ss5 = node.parent ?: return false
        val unnamed = ss5.parent ?: return false
        val otv = unnamed.parent ?: return false
        val actualRow = otv.parent ?: return false
        val actualRowBounds = Rect().also(actualRow::getBoundsInScreen)
        val expectedRowBounds = Rect().also(row::getBoundsInScreen)
        return ss5.viewIdResourceName == "com.tencent.mm:id/ss5" &&
            unnamed.viewIdResourceName.isNullOrEmpty() && unnamed.className?.toString() == ImageCapturePolicy.RowClass &&
            otv.viewIdResourceName == "com.tencent.mm:id/otv" &&
            actualRow.viewIdResourceName == ImageCapturePolicy.RowViewId &&
            actualRow.className?.toString() == ImageCapturePolicy.RowClass && actualRowBounds == expectedRowBounds
    }

    private fun Rect.toCaptureRect(): CaptureRect = CaptureRect(left, top, right, bottom)

    private fun processHistoryForward(active: Session) {
        if (session !== active || !active.cancellation.canAct() || active.phase == Phase.Finishing) return
        if (!active.cancellation.isCommitted() && active.historyIsCurrent?.invoke() != true) {
            finishSession("WECHAT_ACTION_CHANGED", "HISTORY_SOURCE_CHANGED")
            return
        }
        val root = rootInActiveWindow?.takeIf { it.packageName?.toString() == NotificationSnapshot.WechatPackage }
        val nodes = root?.let(::walk).orEmpty().filter { it.isVisibleToUser }
        if (root != null) when (active.phase) {
            Phase.OpeningWechat -> {
                val card = historySourceCard(active, nodes)
                if (card != null) {
                    val hash = historyCardHash(walk(card)) ?: return finishSession("FAILED", "HISTORY_CARD_AMBIGUOUS")
                    captureHistoryTrailingTexts(active, card, nodes)
                    val identity = "${root.windowId}|$hash"
                    val now = SystemClock.uptimeMillis()
                    if (active.historySourceIdentity == null) {
                        active.historySourceIdentity = identity
                        active.historyStableSince = now
                    } else if (active.historySourceIdentity != identity) {
                        return finishSession("WECHAT_ACTION_CHANGED", "HISTORY_CARD_CHANGED")
                    } else if (now - active.historyStableSince >= ViewerSettleMillis) {
                        active.historyCardHash = hash
                        active.historyCardTitle = walk(card).singleOrNull { it.viewIdResourceName == "com.tencent.mm:id/obc" }?.text?.toString()
                        active.phase = Phase.HistoryViewer
                        recordAccessibilityStage("HISTORY_OPEN_CARD")
                        if (active.cancellation.runIfActive { card.performAction(AccessibilityNodeInfo.ACTION_CLICK) } != true) {
                            return finishSession("FAILED", "HISTORY_CARD_OPEN_FAILED")
                        }
                    }
                } else if (active.historySourceIdentity != null) {
                    // A notification can briefly rebuild the list. Keep the pinned identity and restart settling.
                    active.historyStableSince = SystemClock.uptimeMillis()
                }
            }
            Phase.HistoryViewer -> {
                if (isHistoryViewer(active, nodes)) {
                    val more = nodes.singleOrNull { it.viewIdResourceName == "com.tencent.mm:id/fq" &&
                        it.contentDescription?.toString() == "更多信息" && it.isClickable && it.isEnabled }
                    if (more != null) {
                        active.phase = Phase.HistoryMenu
                        if (active.cancellation.runIfActive { more.performAction(AccessibilityNodeInfo.ACTION_CLICK) } != true) {
                            return finishSession("FAILED", "HISTORY_VIEWER_MENU_FAILED")
                        }
                    }
                }
            }
            Phase.HistoryMenu -> {
                val controls = nodes.filter { it.viewIdResourceName == "com.tencent.mm:id/obc" && it.text?.toString() == "发送给朋友" }
                    .mapNotNull(::clickableAncestor).distinctBy(::nodeIdentity)
                if (controls.size > 1) return finishSession("FAILED", "HISTORY_MENU_AMBIGUOUS")
                controls.singleOrNull()?.let { control ->
                    active.phase = Phase.HistorySearch
                    if (active.cancellation.runIfActive { control.performAction(AccessibilityNodeInfo.ACTION_CLICK) } != true) {
                        return finishSession("FAILED", "HISTORY_MENU_CLICK_FAILED")
                    }
                }
            }
            Phase.HistorySearch -> {
                val noticeTitle = nodes.singleOrNull { it.viewIdResourceName == "com.tencent.mm:id/jlo" }
                val noticeBody = nodes.singleOrNull { it.viewIdResourceName == "com.tencent.mm:id/jlg" }
                val acknowledge = nodes.singleOrNull { it.viewIdResourceName == "com.tencent.mm:id/mm_alert_ok_btn" && it.isClickable && it.isEnabled }
                if (ChatHistoryForwardPolicy.isForwardTranslationNotice(noticeTitle?.text, noticeBody?.text, acknowledge?.text)) {
                    if (active.cancellation.runIfActive { acknowledge?.performAction(AccessibilityNodeInfo.ACTION_CLICK) == true } != true) {
                        return finishSession("FAILED", "HISTORY_NOTICE_DISMISS_FAILED")
                    }
                    recordAccessibilityStage("HISTORY_TRANSLATION_NOTICE_DISMISSED")
                }
                if (nodes.any { it.viewIdResourceName == "android:id/text1" && it.text?.toString() == "选择聊天" }) {
                    val input = nodes.singleOrNull { it.viewIdResourceName == "com.tencent.mm:id/k13" && it.isEditable && it.isEnabled }
                    if (input != null) {
                        val arguments = Bundle().apply {
                            putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, ChatHistoryForwardPolicy.TargetGroup)
                        }
                        active.phase = Phase.HistoryResult
                        if (active.cancellation.runIfActive { input.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, arguments) } != true) {
                            return finishSession("FAILED", "HISTORY_SEARCH_FAILED")
                        }
                    }
                }
            }
            Phase.HistoryResult -> {
                val input = nodes.singleOrNull { it.viewIdResourceName == "com.tencent.mm:id/k13" && it.isEditable }
                if (input?.text?.toString() == ChatHistoryForwardPolicy.TargetGroup) {
                    val results = nodes.filter { it.viewIdResourceName == "com.tencent.mm:id/kbq" && it.text?.toString() == ChatHistoryForwardPolicy.TargetGroup }
                        .mapNotNull(::clickableAncestor).distinctBy(::nodeIdentity)
                    if (results.size > 1) return finishSession("FAILED", "HISTORY_GROUP_AMBIGUOUS")
                    val result = results.singleOrNull()
                    if (result != null) {
                        val groupCount = walk(result).count { it.viewIdResourceName == "com.tencent.mm:id/vh8" &&
                            Regex("\\([1-9]\\d*人\\)").matches(it.text?.toString().orEmpty()) }
                        if (groupCount != 1) return finishSession("FAILED", "HISTORY_RESULT_NOT_GROUP")
                        active.phase = Phase.HistoryConfirm
                        if (active.cancellation.runIfActive { result.performAction(AccessibilityNodeInfo.ACTION_CLICK) } != true) {
                            return finishSession("FAILED", "HISTORY_GROUP_CLICK_FAILED")
                        }
                    }
                }
            }
            Phase.HistoryConfirm -> {
                val send = nodes.singleOrNull { it.viewIdResourceName == "com.tencent.mm:id/b08" && it.text?.toString() == "发送" && it.isClickable && it.isEnabled }
                if (send != null) {
                    val recipients = nodes.filter { it.viewIdResourceName == "com.tencent.mm:id/kbq" }
                    if (recipients.size != 1 || !ChatHistoryForwardPolicy.matchesChatTitle(recipients.single().text, ChatHistoryForwardPolicy.TargetGroup) ||
                        historyCardHash(nodes) != active.historyCardHash) {
                        return finishSession("FAILED", "HISTORY_CONFIRM_MISMATCH")
                    }
                    active.phase = Phase.HistoryClaiming
                    val windowId = root.windowId
                    // Persist SENDING off the main thread, then re-read the dialog before the irreversible click.
                    ImageExecutor.execute {
                        val claimed = runCatching { active.historyClaimSend?.invoke() == true }.getOrDefault(false)
                        handler.post {
                            if (session !== active || active.phase != Phase.HistoryClaiming || !active.cancellation.canAct()) return@post
                            val currentRoot = rootInActiveWindow?.takeIf { it.packageName?.toString() == NotificationSnapshot.WechatPackage && it.windowId == windowId }
                            val currentNodes = currentRoot?.let(::walk).orEmpty().filter { it.isVisibleToUser }
                            val currentRecipient = currentNodes.filter { it.viewIdResourceName == "com.tencent.mm:id/kbq" }
                            val currentSend = currentNodes.singleOrNull { it.viewIdResourceName == "com.tencent.mm:id/b08" && it.text?.toString() == "发送" && it.isClickable && it.isEnabled }
                            if (!claimed || currentSend == null || currentRecipient.size != 1 ||
                                !ChatHistoryForwardPolicy.matchesChatTitle(currentRecipient.single().text, ChatHistoryForwardPolicy.TargetGroup) ||
                                historyCardHash(currentNodes) != active.historyCardHash) {
                                finishSession("FAILED", "HISTORY_SEND_UNCERTAIN_NO_RETRY")
                                return@post
                            }
                            active.phase = Phase.HistoryVerify
                            active.clickAt = System.currentTimeMillis()
                            if (!active.cancellation.commitIfActive { active.historyIsCurrent?.invoke() == true && currentSend.performAction(AccessibilityNodeInfo.ACTION_CLICK) }) {
                                finishSession("FAILED", "HISTORY_SEND_UNCERTAIN_NO_RETRY")
                            }
                        }
                    }
                }
            }
            Phase.HistoryVerify -> {
                if (nodes.none { it.viewIdResourceName == "com.tencent.mm:id/b08" } && isHistoryViewer(active, nodes)) {
                    active.phase = Phase.HistoryReturnSource
                    active.clickAt = System.currentTimeMillis()
                    if (!performGlobalAction(GLOBAL_ACTION_BACK)) return finishSession("FAILED", "HISTORY_RETURN_SOURCE_FAILED")
                }
                if (System.currentTimeMillis() - active.clickAt >= AcceptanceTimeoutMillis) {
                    return finishSession("FAILED", "HISTORY_ACCEPTANCE_UNCERTAIN_NO_RETRY")
                }
            }
            Phase.HistoryReturnSource -> {
                val card = historySourceCard(active, nodes)
                if (card != null && historyCardHash(walk(card)) == active.historyCardHash) {
                    captureHistoryTrailingTexts(active, card, nodes)
                    return finishSession("HISTORY_FORWARDED", "FORWARD_ACCEPTED")
                }
                if (System.currentTimeMillis() - active.clickAt >= AcceptanceTimeoutMillis) return finishSession("FAILED", "HISTORY_RETURN_SOURCE_TIMEOUT")
            }
            else -> Unit
        }
        if (!handler.hasCallbacks(historyTick)) handler.postDelayed(historyTick, AcceptancePollMillis)
    }

    private fun historySourceCard(active: Session, nodes: List<AccessibilityNodeInfo>): AccessibilityNodeInfo? {
        val titles = nodes.filter { it.viewIdResourceName in setOf("com.tencent.mm:id/obn", "android:id/text1") &&
            Rect().also(it::getBoundsInScreen).bottom <= WechatChatTopPx }.mapNotNull { it.text?.toString()?.takeIf(String::isNotBlank) }.distinct()
        if (!ChatHistoryForwardPolicy.sourceTitleMatches(titles, active.conversationTitle.concatToString(), active.historySender.orEmpty())) return null
        val input = nodes.singleOrNull(::isBottomComposerInput) ?: return null
        val bottom = Rect().also(input::getBoundsInScreen).top
        val rows = nodes.filter { it.viewIdResourceName == ImageCapturePolicy.RowViewId }.sortedBy { Rect().also(it::getBoundsInScreen).top }
        val historyFlags = rows.map { row -> walk(row).any { it.viewIdResourceName == "com.tencent.mm:id/nec" && it.text?.toString() == "聊天记录" } }
        val index = ChatHistoryForwardPolicy.latestHistoryIndex(historyFlags, active.historyCardsAfter?.invoke() ?: 0) ?: return null
        val row = rows[index]
        val rowNodes = walk(row).filter { it.isVisibleToUser }
        val avatar = rowNodes.singleOrNull { it.viewIdResourceName == ImageCapturePolicy.AvatarViewId &&
            it.contentDescription?.toString() == "${active.historySender}头像" } ?: return null
        val avatarBounds = Rect().also(avatar::getBoundsInScreen)
        // With a hidden title, only a direct sender is accepted; the notification intent and incoming avatar must agree.
        if (avatarBounds.centerX() >= resources.displayMetrics.widthPixels / 2) return null
        val card = rowNodes.singleOrNull { it.viewIdResourceName == "com.tencent.mm:id/st6" && it.isEnabled && it.isClickable } ?: return null
        val bounds = Rect().also(card::getBoundsInScreen)
        if (bounds.top < WechatChatTopPx || bounds.bottom > bottom || bounds.left < avatarBounds.right) return null
        return card.takeIf { historyCardHash(walk(it)) != null }
    }

    private fun isHistoryViewer(active: Session, nodes: List<AccessibilityNodeInfo>): Boolean =
        nodes.any { it.viewIdResourceName == "com.tencent.mm:id/jlt" } &&
            nodes.count { it.viewIdResourceName == "android:id/text1" && it.text?.toString() == active.historyCardTitle } == 1

    private fun captureHistoryTrailingTexts(active: Session, card: AccessibilityNodeInfo, nodes: List<AccessibilityNodeInfo>) {
        val row = messageRowAncestor(card) ?: return
        val bottom = Rect().also(row::getBoundsInScreen).bottom
        val trailing = nodes.filter { it.viewIdResourceName == ImageCapturePolicy.RowViewId && Rect().also(it::getBoundsInScreen).top >= bottom }
            .sortedBy { Rect().also(it::getBoundsInScreen).top }
        val texts = trailing.takeWhile { candidate -> walk(candidate).none { it.viewIdResourceName == "com.tencent.mm:id/nec" && it.text?.toString() == "聊天记录" } }
            .mapNotNull { candidate ->
                val rowNodes = walk(candidate).filter { it.isVisibleToUser }
                val avatar = rowNodes.singleOrNull { it.viewIdResourceName == ImageCapturePolicy.AvatarViewId && it.contentDescription?.toString() == "${active.historySender}头像" }
                if (avatar == null || Rect().also(avatar::getBoundsInScreen).centerX() >= resources.displayMetrics.widthPixels / 2) return@mapNotNull null
                rowNodes.singleOrNull { it.viewIdResourceName == "com.tencent.mm:id/bkl" }?.text?.toString()?.takeIf(String::isNotBlank)
            }
        if (texts != active.historyReportedTexts) {
            active.historyReportedTexts = texts
            active.historyCaptureTexts?.invoke(texts)
        }
    }

    private fun historyCardHash(nodes: List<AccessibilityNodeInfo>): String? {
        if (nodes.count { it.viewIdResourceName == "com.tencent.mm:id/nec" && it.text?.toString() == "聊天记录" } != 1) return null
        val title = nodes.singleOrNull { it.viewIdResourceName == "com.tencent.mm:id/obc" }?.text?.toString()?.takeIf(String::isNotBlank) ?: return null
        val preview = nodes.singleOrNull { it.viewIdResourceName == "com.tencent.mm:id/cu2" }?.text?.toString() ?: return null
        return Privacy.saltedHash("${title.length}:$title$preview", Privacy.salt(this))
    }

    private fun processVoiceTranscription(active: Session) {
        if (session !== active || !active.cancellation.canAct() || active.phase == Phase.Finishing) return
        if (runCatching { active.voiceIsCurrent?.invoke() == true }.getOrDefault(false).not()) {
            finishSession("WECHAT_ACTION_CHANGED", "VOICE_SOURCE_CHANGED")
            return
        }
        val root = rootInActiveWindow?.takeIf { it.packageName?.toString() == NotificationSnapshot.WechatPackage }
        val nodes = root?.let(::walk).orEmpty().filter { it.isVisibleToUser }
        if (root == null && active.phase == Phase.VoiceWaitingTranscript) recordVoiceWait(active, "NO_WECHAT_ROOT")
        if (root != null) when (active.phase) {
            Phase.OpeningWechat -> {
                val source = currentVoiceSource(active, root, nodes)
                if (source != null) {
                    val now = SystemClock.uptimeMillis()
                    if (active.voiceSourceIdentity == null) {
                        active.voiceSourceIdentity = source.identity
                        active.voiceSelection = source.selection
                        active.voiceWindowId = source.windowId
                        active.voiceSourceStableSince = now
                        captureVoiceTrailingTexts(active, source, nodes)
                    } else if (!matchesPinnedVoice(active, source)) {
                        // No action yet: opening a conversation can scroll older rows out of view.
                        active.voiceSourceIdentity = source.identity
                        active.voiceSelection = source.selection
                        active.voiceWindowId = source.windowId
                        active.voiceSourceStableSince = now
                    } else if (now - active.voiceSourceStableSince >= VoiceSourceSettleMillis) {
                        active.voiceSourceNode = source.voice
                        if (walk(source.row).any { it.isVisibleToUser && it.viewIdResourceName in setOf(VoiceTranscriptViewId, VoiceTranscriptContainerViewId) }) {
                            active.phase = Phase.VoiceWaitingTranscript
                            active.clickAt = now
                        } else {
                            active.phase = Phase.VoiceMenu
                            recordAccessibilityStage("VOICE_LONG_CLICK")
                            if (active.cancellation.runIfActive {
                                    source.voice.performAction(AccessibilityNodeInfo.ACTION_LONG_CLICK)
                                } != true) {
                                return finishSession("FAILED", "VOICE_LONG_CLICK_FAILED")
                            }
                        }
                    }
                } else if (active.voiceSourceIdentity != null) {
                    active.voiceSourceStableSince = SystemClock.uptimeMillis()
                }
            }
            Phase.VoiceMenu -> {
                // The popup can have its own window. Revalidate the pinned source after it closes.
                if (active.voiceSelection == null) return finishSession("WECHAT_ACTION_CHANGED", "VOICE_SOURCE_CHANGED")
                val controls = nodes.filter { it.viewIdResourceName == "com.tencent.mm:id/obc" &&
                    it.text?.toString() == "转文字" }
                    .mapNotNull(::clickableAncestor)
                    .distinctBy(::nodeIdentity)
                if (controls.size > 1) return finishSession("WECHAT_ACTION_CHANGED", "VOICE_MENU_AMBIGUOUS")
                controls.singleOrNull()?.let { control ->
                    active.phase = Phase.VoiceWaitingTranscript
                    active.voiceTranscriptStableSince = 0L
                    active.clickAt = SystemClock.uptimeMillis()
                    if (active.cancellation.runIfActive {
                            control.performAction(AccessibilityNodeInfo.ACTION_CLICK)
                        } != true) {
                        return finishSession("FAILED", "VOICE_MENU_CLICK_FAILED")
                    }
                    recordAccessibilityStage("VOICE_TRANSCRIPTION_REQUESTED")
                }
            }
            Phase.VoiceWaitingTranscript -> {
                val source = currentVoiceSource(active, root, nodes)
                if (source == null) recordVoiceWait(active, "NO_SOURCE")
                if (source == null && SystemClock.uptimeMillis() - maxOf(active.clickAt, active.voiceRevealAt) >= VoiceSourceReturnTimeoutMillis) {
                    return finishSession("WECHAT_ACTION_CHANGED", "VOICE_SOURCE_CHANGED")
                }
                if (source != null && !matchesPinnedVoice(active, source, allowClippedHistory = true)) {
                    return finishSession("WECHAT_ACTION_CHANGED", "VOICE_SOURCE_CHANGED")
                }
                if (source != null) {
                    captureVoiceTrailingTexts(active, source, nodes)
                    val transcriptNodes = walk(source.row).filter {
                        it.isVisibleToUser && it.viewIdResourceName == VoiceTranscriptViewId
                    }.mapNotNull { it.text?.toString()?.trim()?.takeIf(String::isNotBlank) }
                    if (transcriptNodes.size > 1) return finishSession("WECHAT_ACTION_CHANGED", "VOICE_TRANSCRIPT_AMBIGUOUS")
                    val current = transcriptNodes.singleOrNull()
                    val containerCount = walk(source.row).count { it.isVisibleToUser && it.viewIdResourceName == VoiceTranscriptContainerViewId }
                    recordVoiceWait(active, "SOURCE_TEXT_${current != null}_CONTAINERS_$containerCount")
                    if (current == null && SystemClock.uptimeMillis() - active.clickAt >= 2_000L) {
                        val container = walk(source.row).singleOrNull {
                            it.isVisibleToUser && it.viewIdResourceName == VoiceTranscriptContainerViewId
                        }
                        if (container != null) {
                            active.voiceCopyAt = System.currentTimeMillis()
                            if (container.performAction(AccessibilityNodeInfo.ACTION_COPY)) {
                                return readVoiceClipboard(active)
                            }
                            val bounds = Rect().also(container::getBoundsInScreen)
                            if (!source.viewport.contains(bounds)) {
                                if (active.voiceRevealAt == 0L) {
                                    active.voiceRevealAt = SystemClock.uptimeMillis()
                                    if (!source.row.performAction(AccessibilityNodeInfo.AccessibilityAction.ACTION_SHOW_ON_SCREEN.id)) {
                                        return finishSession("FAILED", "VOICE_TRANSCRIPT_REVEAL_FAILED")
                                    }
                                    recordAccessibilityStage("VOICE_TRANSCRIPT_REVEAL_REQUESTED")
                                } else if (SystemClock.uptimeMillis() - active.voiceRevealAt >= VoiceSourceReturnTimeoutMillis) {
                                    return finishSession("FAILED", "VOICE_TRANSCRIPT_OUTSIDE_VIEWPORT")
                                }
                                if (!handler.hasCallbacks(historyTick)) handler.postDelayed(historyTick, AcceptancePollMillis)
                                return
                            }
                            active.phase = Phase.VoiceLongPressing
                            recordAccessibilityStage("VOICE_TEXT_LONG_CLICK")
                            val gesture = GestureDescription.Builder().addStroke(GestureDescription.StrokeDescription(
                                Path().apply { moveTo(bounds.exactCenterX(), bounds.exactCenterY()) }, 0, 650,
                            )).build()
                            if (!dispatchGesture(gesture, object : GestureResultCallback() {
                                override fun onCompleted(gestureDescription: GestureDescription?) {
                                    if (session !== active || active.phase != Phase.VoiceLongPressing || !active.cancellation.canAct()) return
                                    active.phase = Phase.VoiceCopyMenu
                                    active.clickAt = SystemClock.uptimeMillis()
                                    recordAccessibilityStage("VOICE_TEXT_LONG_CLICK_COMPLETED")
                                    processVoiceTranscription(active)
                                }

                                override fun onCancelled(gestureDescription: GestureDescription?) {
                                    if (session === active && active.phase == Phase.VoiceLongPressing) finishSession("FAILED", "VOICE_TEXT_LONG_CLICK_CANCELLED")
                                }
                            }, handler)) return finishSession("FAILED", "VOICE_TEXT_LONG_CLICK_FAILED")
                        }
                    }
                    if (current != null && VoiceTranscriptionPolicy.isKnownFailureText(current)) {
                        return finishSession("FAILED", "VOICE_TRANSCRIPTION_FAILED")
                    }
                    val now = SystemClock.uptimeMillis()
                    if (current != active.voiceTranscript) {
                        active.voiceTranscript = current
                        active.voiceTranscriptStableSince = now
                    } else if (VoiceTranscriptionPolicy.hasStableTranscript(
                            current, active.voiceTranscript, active.voiceTranscriptStableSince, now, VoiceTranscriptSettleMillis,
                        )) {
                        return finishSession("VOICE_TRANSCRIBED", "TRANSCRIPT_CAPTURED")
                    }
                }
            }
            Phase.VoiceCopyMenu -> {
                val source = currentVoiceSource(active, root, nodes)
                if (source != null && !matchesPinnedVoice(active, source, allowClippedHistory = true)) return finishSession("WECHAT_ACTION_CHANGED", "VOICE_SOURCE_CHANGED")
                val allNodes = rootsForPackage(NotificationSnapshot.WechatPackage).flatMap(::walk).filter { it.isVisibleToUser }
                val copy = allNodes.filter { it.text?.toString() == "复制" || it.contentDescription?.toString() == "复制" }
                    .mapNotNull(::clickableAncestor).distinctBy(::nodeIdentity)
                if (copy.size > 1) return finishSession("WECHAT_ACTION_CHANGED", "VOICE_COPY_AMBIGUOUS")
                if (copy.size == 1) {
                    active.voiceCopyAt = System.currentTimeMillis()
                    if (copy.single().performAction(AccessibilityNodeInfo.ACTION_CLICK)) return readVoiceClipboard(active)
                    return finishSession("FAILED", "VOICE_COPY_CLICK_FAILED")
                }
                if (SystemClock.uptimeMillis() - active.clickAt >= 1_000L && source != null) {
                    val container = walk(source.row).singleOrNull { it.viewIdResourceName == VoiceTranscriptContainerViewId }
                    active.voiceCopyAt = System.currentTimeMillis()
                    if (container?.performAction(AccessibilityNodeInfo.ACTION_COPY) == true) return readVoiceClipboard(active)
                    return finishSession("FAILED", "VOICE_COPY_CONTROL_UNAVAILABLE")
                }
            }
            else -> Unit
        }
        if (!handler.hasCallbacks(historyTick)) handler.postDelayed(historyTick, AcceptancePollMillis)
    }

    private fun recordVoiceWait(active: Session, observation: String) {
        if (active.voiceWaitObservation == observation) return
        active.voiceWaitObservation = observation
        recordAccessibilityStage("VOICE_WAIT_$observation")
    }

    private fun readVoiceClipboard(active: Session) {
        active.phase = Phase.VoiceClipboardReading
        recordAccessibilityStage("VOICE_COPY_REQUESTED")
        VoiceClipboardActivity.start(this, active.voiceCopyAt,
            { session === active && active.cancellation.canAct() && active.voiceIsCurrent?.invoke() == true },
        ) { text ->
            if (session !== active || active.phase != Phase.VoiceClipboardReading) return@start
            if (text.isNullOrBlank() || VoiceTranscriptionPolicy.isKnownFailureText(text)) {
                finishSession("FAILED", "VOICE_CLIPBOARD_INVALID")
            } else {
                active.voiceTranscript = text
                finishSession("VOICE_TRANSCRIBED", "CLIPBOARD_CAPTURED")
            }
        }
    }

    private fun currentVoiceSource(
        active: Session,
        root: AccessibilityNodeInfo,
        nodes: List<AccessibilityNodeInfo>,
    ): VoiceSourceSelection? {
        val titles = nodes.filter { it.viewIdResourceName in setOf("com.tencent.mm:id/obn", "android:id/text1") &&
            Rect().also(it::getBoundsInScreen).bottom <= WechatChatTopPx }
            .mapNotNull { it.text?.toString()?.takeIf(String::isNotBlank) }
            .distinct()
        val sender = active.voiceSender ?: return null
        if (!ChatHistoryForwardPolicy.sourceTitleMatches(titles, active.conversationTitle.concatToString(), sender)) return null
        val composer = nodes.singleOrNull(::isBottomComposerInput) ?: return null
        val bottom = Rect().also(composer::getBoundsInScreen).top
        val viewport = Rect(0, WechatChatTopPx, resources.displayMetrics.widthPixels, bottom)
        val rows = nodes.filter { it.viewIdResourceName == ImageCapturePolicy.RowViewId }
            .filter { row ->
                val bounds = Rect().also(row::getBoundsInScreen)
                VoiceTranscriptionPolicy.intersectsViewport(bounds.top, bounds.bottom, viewport.top, viewport.bottom)
            }
            .sortedBy { Rect().also(it::getBoundsInScreen).top }
        val sources = rows.mapNotNull { voiceRowMarker(it, viewport) }
        val voicesAfter = runCatching { active.voiceVoicesAfter?.invoke() }.getOrNull() ?: return null
        val selection = VoiceTranscriptionPolicy.pinVoiceSelection(sources.map(VoiceRowMarker::signature), voicesAfter)
            ?: return null
        val source = sources[selection.selectedIndex]
        if (source.avatar.contentDescription?.toString() != "${sender}头像") return null
        if (active.voiceExpectedDuration != null && walk(source.row).singleOrNull { it.viewIdResourceName == VoiceDurationViewId }?.text?.toString() != active.voiceExpectedDuration) return null
        return VoiceSourceSelection(root.windowId, source.signature, selection, source.row, source.voice, viewport)
    }

    private fun incomingVoiceNode(row: AccessibilityNodeInfo): AccessibilityNodeInfo? =
        voiceRowMarker(row)?.voice

    private fun voiceRowMarker(row: AccessibilityNodeInfo, viewport: Rect? = null): VoiceRowMarker? {
        val rowNodes = walk(row).filter { it.isVisibleToUser &&
            (viewport == null || Rect.intersects(viewport, Rect().also(it::getBoundsInScreen))) }
        val voice = rowNodes.singleOrNull { node ->
            node.isEnabled && node.viewIdResourceName == VoiceBubbleViewId
        } ?: return null
        val avatar = rowNodes.singleOrNull { it.viewIdResourceName == ImageCapturePolicy.AvatarViewId } ?: return null
        if (Rect().also(avatar::getBoundsInScreen).centerX() >= resources.displayMetrics.widthPixels / 2) return null
        val duration = rowNodes.singleOrNull { it.viewIdResourceName == VoiceDurationViewId }
            ?.text?.toString()?.takeIf(VoiceTranscriptionPolicy::isVoiceDuration) ?: return null
        // WeChat clears the voice description after conversion/read; avatar, duration and row order remain stable.
        return VoiceRowMarker("${avatar.contentDescription}|$duration", row, voice, avatar)
    }

    private fun matchesPinnedVoice(active: Session, source: VoiceSourceSelection, allowClippedHistory: Boolean = false): Boolean =
        source.windowId == active.voiceWindowId && source.identity == active.voiceSourceIdentity &&
            (!allowClippedHistory || active.voiceSourceNode == source.voice) &&
            active.voiceSelection?.let { VoiceTranscriptionPolicy.matchesPinnedVoice(
                it, source.selection.signatures, source.selection.voicesAfter,
                sameVoiceNode = allowClippedHistory && active.voiceSourceNode == source.voice,
            ) } == true

    private fun captureVoiceTrailingTexts(
        active: Session,
        source: VoiceSourceSelection,
        nodes: List<AccessibilityNodeInfo>,
    ) {
        val sourceBottom = Rect().also(source.row::getBoundsInScreen).bottom
        val sender = active.voiceSender ?: return
        val texts = ArrayList<String>()
        val trailing = nodes.filter { it.viewIdResourceName == ImageCapturePolicy.RowViewId &&
            Rect().also(it::getBoundsInScreen).top >= sourceBottom }
            .sortedBy { Rect().also(it::getBoundsInScreen).top }
        for (row in trailing) {
            if (incomingVoiceNode(row) != null) break
            val rowNodes = walk(row).filter { it.isVisibleToUser }
            val avatar = rowNodes.singleOrNull { it.viewIdResourceName == ImageCapturePolicy.AvatarViewId &&
                it.contentDescription?.toString() == "${sender}头像" } ?: break
            if (Rect().also(avatar::getBoundsInScreen).centerX() >= resources.displayMetrics.widthPixels / 2) break
            val text = rowNodes.singleOrNull { it.viewIdResourceName == VoiceDurationViewId }
                ?.text?.toString()?.trim()?.takeIf(String::isNotBlank) ?: break
            if (!text.startsWith("[") && !VoiceTranscriptionPolicy.isVoiceDuration(text) &&
                text != active.voiceTranscript && !VoiceTranscriptionPolicy.isKnownFailureText(text)) {
                texts += text
            }
        }
        if (texts != active.voiceReportedTexts) {
            active.voiceReportedTexts = texts
            active.voiceCaptureTexts?.invoke(texts)
        }
    }

    private fun processConversationSearch(active: Session, roots: List<AccessibilityNodeInfo>) {
        if (roots.isEmpty()) return
        val nodes = roots.flatMap(::walk)
        when (active.phase) {
            Phase.FindingSearch -> {
                val editableInputs = nodes.count(::isBottomComposerInput)
                if (LockscreenReplySelectors.isUnambiguousChatSurface(
                        visibleTitleMatches(roots, active.expectedTitleHash),
                        editableInputs,
                    )) {
                    active.phase = Phase.OpeningWechat
                    processWechatWindow(active)
                    return
                }
                val candidates = nodes.filter { node ->
                    node.isVisibleToUser && node.isEnabled && node.isClickable &&
                        nodeLabels(node).any { LockscreenReplySelectors.normalizeTitle(it) in SearchLabels }
                }
                if (candidates.size > 1) return finishSession("WECHAT_ACTION_CHANGED", "AMBIGUOUS_SEARCH_CONTROL")
                val search = candidates.singleOrNull() ?: return
                if (active.cancellation.runIfActive { search.performAction(AccessibilityNodeInfo.ACTION_CLICK) } != true) {
                    finishSession("FAILED", "SEARCH_CLICK_FAILED")
                    return
                }
                active.phase = Phase.EnteringSearch
            }
            Phase.EnteringSearch -> {
                val inputs = nodes.filter { it.isVisibleToUser && it.isEnabled && it.isEditable }
                if (inputs.size > 1) return finishSession("WECHAT_ACTION_CHANGED", "AMBIGUOUS_SEARCH_INPUT")
                val input = inputs.singleOrNull() ?: return
                val arguments = Bundle().apply {
                    putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, active.conversationTitle.concatToString())
                }
                if (active.cancellation.runIfActive { input.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, arguments) } != true) {
                    finishSession("FAILED", "SEARCH_TEXT_FAILED")
                    return
                }
                active.phase = Phase.SelectingResult
                handler.postDelayed({
                    if (session === active && active.phase == Phase.SelectingResult) processWechatWindow(active)
                }, UiSettleMillis)
            }
            Phase.SelectingResult -> {
                val normalizedTitle = LockscreenReplySelectors.normalizeTitle(active.conversationTitle.concatToString())
                val results = nodes.asSequence()
                    .filter { node ->
                        node.isVisibleToUser && !node.isEditable &&
                            nodeLabels(node).any { LockscreenReplySelectors.normalizeTitle(it) == normalizedTitle }
                    }
                    .mapNotNull(::clickableAncestor)
                    .distinctBy(::nodeIdentity)
                    .toList()
                if (results.size > 1) return finishSession("WECHAT_ACTION_CHANGED", "AMBIGUOUS_SEARCH_RESULT")
                val result = results.singleOrNull() ?: return
                if (active.cancellation.runIfActive { result.performAction(AccessibilityNodeInfo.ACTION_CLICK) } != true) {
                    finishSession("FAILED", "SEARCH_RESULT_CLICK_FAILED")
                    return
                }
                active.phase = Phase.OpeningWechat
            }
            else -> Unit
        }
    }

    private fun isBottomComposerInput(node: AccessibilityNodeInfo): Boolean {
        if (!node.isVisibleToUser || !node.isEnabled || !node.isEditable) return false
        val bounds = Rect().also(node::getBoundsInScreen)
        return LockscreenReplySelectors.isBottomComposerBounds(
            bounds.left,
            bounds.top,
            bounds.right,
            bounds.bottom,
            resources.displayMetrics.heightPixels,
        )
    }

    private fun isSameRowRightSend(input: AccessibilityNodeInfo, send: AccessibilityNodeInfo): Boolean {
        val inputBounds = Rect().also(input::getBoundsInScreen)
        val sendBounds = Rect().also(send::getBoundsInScreen)
        return LockscreenReplySelectors.isSameRowRightSend(
            inputBounds.left,
            inputBounds.top,
            inputBounds.right,
            inputBounds.bottom,
            sendBounds.left,
            sendBounds.top,
            sendBounds.right,
            sendBounds.bottom,
        )
    }

    private fun clickableAncestor(node: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        var current: AccessibilityNodeInfo? = node
        while (current != null) {
            if (current.isVisibleToUser && current.isEnabled && current.isClickable) return current
            current = current.parent
        }
        return null
    }

    private fun nodeIdentity(node: AccessibilityNodeInfo): String {
        val bounds = Rect().also(node::getBoundsInScreen)
        return "${node.viewIdResourceName.orEmpty()}|${bounds.left},${bounds.top},${bounds.right},${bounds.bottom}"
    }

    private fun visibleTitleMatches(roots: List<AccessibilityNodeInfo>, expectedHash: String): Boolean {
        val topBoundary = (resources.displayMetrics.heightPixels * TitleRegionFraction).toInt()
        val matchingNodes = roots.flatMap(::walk).count { node ->
            if (!node.isVisibleToUser) return@count false
            val bounds = Rect().also(node::getBoundsInScreen)
            if (bounds.top >= topBoundary) return@count false
            nodeLabels(node)
                .map(LockscreenReplySelectors::normalizeTitle)
                .filter(String::isNotEmpty)
                .distinct()
                .map { Privacy.saltedHash(it, Privacy.salt(this)) }
                .any { it == expectedHash }
        }
        return LockscreenReplySelectors.titleMatches(expectedHash, List(matchingNodes) { expectedHash })
    }

    private fun rootsForPackage(packageName: String): List<AccessibilityNodeInfo> =
        windows.mapNotNull { it.root }.filter { it.packageName?.toString() == packageName }

    private fun hasExactSystemPinKeyId(node: AccessibilityNodeInfo): Boolean =
        node.packageName?.toString() == SystemUiPackage && node.viewIdResourceName in SystemPinKeyViewIds

    private fun foregroundPackage(): String? = rootInActiveWindow?.packageName?.toString()

    private fun walk(root: AccessibilityNodeInfo): List<AccessibilityNodeInfo> {
        val nodes = ArrayList<AccessibilityNodeInfo>()
        val pending = ArrayDeque<AccessibilityNodeInfo>()
        pending.add(root)
        while (pending.isNotEmpty()) {
            val node = pending.removeFirst()
            nodes += node
            for (index in 0 until node.childCount) node.getChild(index)?.let(pending::addLast)
        }
        return nodes
    }

    private fun nodeLabels(node: AccessibilityNodeInfo): List<CharSequence> =
        listOfNotNull(node.text, node.contentDescription)

    private fun ancestorViewIds(node: AccessibilityNodeInfo): List<String> {
        val ids = ArrayList<String>()
        var parent = node.parent
        while (parent != null) {
            parent.viewIdResourceName?.let(ids::add)
            parent = parent.parent
        }
        return ids
    }

    private fun finishSession(status: String, stage: String) {
        val active = session ?: return
        if (active.phase == Phase.Finishing) return
        handler.removeCallbacks(pinTick)
        handler.removeCallbacks(historyTick)
        val terminalStatus = if (active.cancellation.canAct()) status else "FAILED"
        val terminalStage = active.cancellation.stage() ?: stage
        active.phase = Phase.Finishing
        VoiceClipboardActivity.cancel()
        UnlockGateActivity.closeGate()
        val currentlyLocked = getSystemService(KeyguardManager::class.java).isDeviceLocked
        val currentForegroundPackage = runCatching(::foregroundPackage).getOrNull()
        val shouldRequestHome = if (active.isVoiceTranscription) {
            VoiceTranscriptionPolicy.shouldRequestHome(active.startedLocked, active.unlockedForWorkflow,
                currentForegroundPackage == NotificationSnapshot.WechatPackage || currentForegroundPackage == packageName)
        } else if (active.isImageCapture || active.isHistoryForward) {
            !active.startedLocked && terminalStatus in setOf("IMAGE_CAPTURED", "HISTORY_FORWARDED", "VOICE_TRANSCRIBED") &&
                currentForegroundPackage == NotificationSnapshot.WechatPackage
        } else AccessibilityReplyPolicy.shouldRequestHome(
                active.startedLocked,
                terminalStatus,
                active.initialForegroundPackage,
                currentForegroundPackage,
                NotificationSnapshot.WechatPackage,
            )
        if (shouldRequestHome) {
            try {
                recordAccessibilityStage("HOME_REQUESTED")
                if (!performGlobalAction(GLOBAL_ACTION_HOME)) recordAccessibilityStage("HOME_REJECTED")
            } catch (_: Exception) {
                recordAccessibilityStage("HOME_REJECTED")
            }
        }
        if (AccessibilityReplyPolicy.shouldRelock(active.startedLocked, currentlyLocked)) {
            performGlobalAction(GLOBAL_ACTION_LOCK_SCREEN)
            pollRelock(active, terminalStatus, terminalStage, System.currentTimeMillis() + RelockTimeoutMillis)
        } else {
            completeSession(active, terminalStatus, terminalStage, finalized = AccessibilityReplyPolicy.finalized(active.startedLocked, currentlyLocked))
        }
    }

    private fun pollRelock(active: Session, status: String, stage: String, deadline: Long) {
        if (getSystemService(KeyguardManager::class.java).isDeviceLocked) {
            completeSession(active, status, stage, finalized = true)
        } else if (System.currentTimeMillis() >= deadline) {
            completeSession(active, "FAILED", "RELOCK_TIMEOUT", finalized = false)
        } else {
            handler.postDelayed({ pollRelock(active, status, stage, deadline) }, RelockPollMillis)
        }
    }

    private fun completeSession(active: Session, status: String, stage: String, finalized: Boolean) {
        if (session !== active) return
        if (active.isHistoryForward && ChatHistoryForwardPolicy.canFinalizeHistoryAttempt(active.startedLocked, active.unlockedForWorkflow, finalized)) {
            if (!pinStore.completeSuccessfulFinalization()) recordAccessibilityStage("HISTORY_ATTEMPT_FINALIZE_FAILED")
        }
        if (active.isVoiceTranscription && VoiceTranscriptionPolicy.canFinalizeVoiceAttempt(active.startedLocked, active.unlockedForWorkflow, finalized)) {
            if (!pinStore.completeSuccessfulFinalization()) recordAccessibilityStage("VOICE_ATTEMPT_FINALIZE_FAILED")
        }
        if (status in setOf("SENT_TO_WECHAT", "HISTORY_FORWARDED") && finalized) {
            if (active.startedLocked && !active.isHistoryForward) successfulLockedRunAwaitingAck = true
            active.result = AccessibilityReplyResult(status, stage, startedLocked = active.startedLocked)
        } else if (status == "IMAGE_CAPTURED" && finalized && active.capturedMedia != null) {
            active.result = AccessibilityReplyResult(status, stage, active.capturedMedia, active.startedLocked)
        } else if (status == "VOICE_TRANSCRIBED" && finalized && !active.voiceTranscript.isNullOrBlank()) {
            active.result = AccessibilityReplyResult(status, stage, startedLocked = active.startedLocked, transcript = active.voiceTranscript)
        } else {
            active.capturedMedia?.bytes?.fill(0)
            active.result = AccessibilityReplyResult(
                if (status in setOf("SENT_TO_WECHAT", "IMAGE_CAPTURED", "HISTORY_FORWARDED", "VOICE_TRANSCRIBED")) "FAILED" else status,
                if (status in setOf("SENT_TO_WECHAT", "IMAGE_CAPTURED", "HISTORY_FORWARDED", "VOICE_TRANSCRIBED")) "FINALIZATION_FAILED" else stage,
                startedLocked = active.startedLocked,
            )
        }
        active.pin.fill('\u0000')
        active.conversationTitle.fill('\u0000')
        active.replyText?.fill('\u0000')
        active.imageSender?.fill('\u0000')
        session = null
        active.completion.countDown()
    }

    companion object {
        private const val SystemUiPackage = "com.android.systemui"
        private val SystemPinKeyViewIds = (0..9).mapTo(HashSet<String>()) { "$SystemUiPackage:id/key$it" }
        private const val UnlockTimeoutMillis = 15_000L
        private const val WechatWindowTimeoutMillis = 8_000L
        private const val WorkflowTimeoutMillis = 32_000L
        private const val RelockTimeoutMillis = 3_000L
        private const val RelockPollMillis = 100L
        private const val PinDigitDelayMillis = 120L
        private const val UiSettleMillis = 180L
        private const val AcceptancePollMillis = 200L
        private const val AcceptanceTimeoutMillis = 3_000L
        private const val InputTextObservationTimeoutMillis = 1_000L
        private const val InputTextObservationPollMillis = 100L
        private const val ImageStabilityMillis = 500L
        private const val ImageStabilityPollMillis = 100L
        private const val OriginalUiPollMillis = 150L
        private const val ViewerOpenTimeoutMillis = 5_000L
        private const val ViewerSettleMillis = 450L
        private const val OriginalViewTimeoutMillis = 5_000L
        private const val OriginalViewSettleMillis = 1_000L
        private const val VoiceSourceSettleMillis = 450L
        private const val ContactsPollMillis = 260L
        private const val ContactsWorkflowTimeoutMillis = 15 * 60_000L
        private const val ContactsLaunchTimeoutMillis = 8_000L
        private const val MaxContactsScrolls = 2_000
        private const val MaxContactsTopResetAttempts = 64
        private const val ContactsStableMillis = 300L
        private const val ContactsTabViewId = "com.tencent.mm:id/icon_tv"
        private const val AndroidTitleViewId = "android:id/text1"
        private const val ContactsRecyclerViewId = "com.tencent.mm:id/mg"
        private const val ContactsNameViewId = "com.tencent.mm:id/kbq"
        private const val ContactsTableViewId = "com.tencent.mm:id/kbo"
        private const val ContactsFooterViewId = "com.tencent.mm:id/caj"
        private const val ContactsNewFriendViewId = "com.tencent.mm:id/obc"
        private const val ContactsTitle = "通讯录"
        private const val ContactsNewFriendTitle = "新的朋友"
        private val ContactsFooter = Regex("(\\d+)个朋友")
        private const val VoiceSourceReturnTimeoutMillis = 3_000L
        private const val VoiceTranscriptSettleMillis = 450L
        private const val WechatChatTopPx = 284
        private const val TitleRegionFraction = 0.45f
        private const val VoiceBubbleViewId = "com.tencent.mm:id/brp"
        private const val VoiceDurationViewId = "com.tencent.mm:id/bkl"
        private const val VoiceTranscriptViewId = "com.tencent.mm:id/brv"
        private const val VoiceTranscriptContainerViewId = "com.tencent.mm:id/bru"
        private val SendLabels = setOf("发送", "Send")
        private val SearchLabels = setOf("搜索", "Search")
        private val ImageExecutor = Executors.newSingleThreadExecutor { runnable -> Thread(runnable, "relay-image-capture") }
        private val ContactsExecutor = Executors.newSingleThreadExecutor { runnable -> Thread(runnable, "relay-contacts-sync") }

        @Volatile private var liveService: LockscreenAccessibilityReplyService? = null
        @Volatile private var successfulLockedRunAwaitingAck = false

        fun isLive(): Boolean = liveService != null

        fun startContactsScan(profile: ContactsProfile): String =
            liveService?.startContactsScan(profile) ?: "Accessibility 服务未连接"

        fun cancelContactsScan() {
            liveService?.contactScan?.let { active -> liveService?.cancelContactsScan(active, "USER_CANCELLED") }
        }

        fun execute(contentIntent: PendingIntent, expectedTitleHash: String, replyText: String): AccessibilityReplyResult =
            liveService?.executeBlocking(contentIntent, expectedTitleHash, null, 0, -1L, replyText)
                ?: AccessibilityReplyResult("REMOTE_INPUT_UNSUPPORTED", "ACCESSIBILITY_NOT_LIVE")

        fun executeConversationSend(contentIntent: PendingIntent?, expectedTitleHash: String, conversationTitle: String, wechatUserId: Int, wechatUserSerial: Long, replyText: String): AccessibilityReplyResult =
            liveService?.executeBlocking(contentIntent, expectedTitleHash, conversationTitle, wechatUserId, wechatUserSerial, replyText)
                ?: AccessibilityReplyResult("REMOTE_INPUT_UNSUPPORTED", "ACCESSIBILITY_NOT_LIVE")

        fun executeImage(
            contentIntent: PendingIntent,
            expectedTitleHash: String,
            sender: String,
            wechatUserId: Int,
            wechatUserSerial: Long,
            isCurrent: () -> Boolean,
        ): AccessibilityReplyResult = liveService?.executeBlocking(
            contentIntent = contentIntent,
            expectedTitleHash = expectedTitleHash,
            conversationTitle = null,
            wechatUserId = wechatUserId,
            wechatUserSerial = wechatUserSerial,
            replyText = null,
            imageSender = sender,
            imageIsCurrent = isCurrent,
        ) ?: AccessibilityReplyResult("REMOTE_INPUT_UNSUPPORTED", "ACCESSIBILITY_NOT_LIVE")

        fun executeHistoryForward(
            contentIntent: PendingIntent?, expectedTitleHash: String, conversationTitle: String, sender: String,
            wechatUserId: Int, wechatUserSerial: Long, isCurrent: () -> Boolean, claimSend: () -> Boolean,
            cardsAfter: () -> Int, captureTexts: (List<String>) -> Unit,
        ): AccessibilityReplyResult = liveService?.executeBlocking(
            contentIntent, expectedTitleHash, conversationTitle, wechatUserId, wechatUserSerial, null,
            historySender = sender, historyIsCurrent = isCurrent, historyClaimSend = claimSend,
            historyCardsAfter = cardsAfter, historyCaptureTexts = captureTexts,
        ) ?: AccessibilityReplyResult("REMOTE_INPUT_UNSUPPORTED", "ACCESSIBILITY_NOT_LIVE")

        fun executeVoiceTranscription(
            contentIntent: PendingIntent?, expectedTitleHash: String, conversationTitle: String, sender: String,
            wechatUserId: Int, wechatUserSerial: Long, isCurrent: () -> Boolean, voicesAfter: () -> Int,
            captureTexts: (List<String>) -> Unit, expectedDuration: String? = null,
        ): AccessibilityReplyResult = liveService?.executeBlocking(
            contentIntent, expectedTitleHash, conversationTitle, wechatUserId, wechatUserSerial, null,
            voiceSender = sender, voiceIsCurrent = isCurrent, voiceVoicesAfter = voicesAfter,
            voiceExpectedDuration = expectedDuration,
            voiceCaptureTexts = captureTexts,
        ) ?: AccessibilityReplyResult("REMOTE_INPUT_UNSUPPORTED", "ACCESSIBILITY_NOT_LIVE")

        fun requiresGateDismiss(): Boolean = liveService?.session?.startedLocked == true

        @Synchronized
        fun consumeSuccessfulLockedRun(): Boolean {
            val completed = successfulLockedRunAwaitingAck
            successfulLockedRunAwaitingAck = false
            return completed
        }

        fun onGateUnlocked(activity: Activity): Boolean = liveService?.launchWechatFromGate(activity) ?: false

        fun runGateAction(action: () -> Unit): Boolean =
            liveService?.session?.cancellation?.runIfActive {
                action()
                true
            } == true

        fun onGateFocused() {
            val service = liveService ?: return
            val active = service.session?.takeIf { it.cancellation.canAct() && it.phase == Phase.Unlocking } ?: return
            if (!active.gateFocusedLogged) {
                active.gateFocusedLogged = true
                service.recordAccessibilityStage("GATE_FOCUSED")
            }
        }

        fun onGateDismissRequested() {
            val service = liveService ?: return
            val active = service.session?.takeIf { it.cancellation.canAct() && it.phase == Phase.Unlocking } ?: return
            active.gateDismissRequested = true
            if (!active.dismissRequestedLogged) {
                active.dismissRequestedLogged = true
                service.recordAccessibilityStage("DISMISS_REQUESTED")
            }
            service.schedulePinTick(active)
        }

        fun onGateDismissSucceeded() {
            val service = liveService ?: return
            val active = service.session?.takeIf { it.cancellation.canAct() && it.phase == Phase.Unlocking } ?: return
            if (!active.dismissSucceededLogged) {
                active.dismissSucceededLogged = true
                service.recordAccessibilityStage("DISMISS_SUCCEEDED")
            }
        }

        fun onGateFailure(stage: String) {
            liveService?.handler?.post { liveService?.finishSession("FAILED", stage) }
        }

        fun abortForDisarm() {
            val service = liveService ?: return
            val active = service.session ?: return
            service.cancelSession(active, "USER_DISARMED")
        }
    }
}
