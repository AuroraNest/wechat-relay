package com.aurora.wechatrelay.probe

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.AccessibilityServiceInfo
import android.app.Activity
import android.app.KeyguardManager
import android.app.PendingIntent
import android.content.Intent
import android.content.pm.LauncherApps
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Paint
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
)

class LockscreenAccessibilityReplyService : AccessibilityService() {
    private val handler = Handler(Looper.getMainLooper())
    private val pinStore by lazy { LockscreenPinStore(this) }
    private val pinTick = Runnable { session?.let(::clickNextPinDigit) }
    @Volatile private var session: Session? = null

    private enum class Phase {
        Unlocking, FindingSearch, EnteringSearch, SelectingResult, OpeningWechat,
        OpeningImageViewer, WaitingOriginalView,
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

    private data class Session(
        val contentIntent: PendingIntent?,
        val expectedTitleHash: String,
        val conversationTitle: CharArray,
        val wechatUserId: Int,
        val wechatUserSerial: Long,
        val replyText: CharArray?,
        val imageSender: CharArray?,
        val imageIsCurrent: (() -> Boolean)?,
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
        @Volatile var result: AccessibilityReplyResult? = null,
    ) {
        val isImageCapture: Boolean get() = imageSender != null
    }

    override fun onServiceConnected() {
        liveService = this
        ProbeRuntime.accessibilityConnected = true
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        val active = session ?: return
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
                )) {
                processWechatWindow(active)
            }
        }
    }

    override fun onInterrupt() {
        val active = session ?: return
        if (active.cancellation.cancel("ACCESSIBILITY_INTERRUPTED")) {
            finishSession("FAILED", "ACCESSIBILITY_INTERRUPTED")
        }
    }

    override fun onDestroy() {
        ProbeRuntime.accessibilityConnected = false
        if (liveService === this) liveService = null
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
    ): AccessibilityReplyResult {
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
            (!isImageCapture && replyText == null)
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
            handler.postDelayed({
                if (session === active && active.phase !in setOf(
                        Phase.OpeningImageViewer,
                        Phase.WaitingOriginalView,
                        Phase.ReadyToClick,
                        Phase.Verifying,
                        Phase.Capturing,
                        Phase.Finishing,
                    )) {
                    finishSession("FAILED", if (active.isImageCapture) "WECHAT_WINDOW_TIMEOUT_OPENING_CHAT" else "WECHAT_WINDOW_TIMEOUT")
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

    private fun processWechatWindow(active: Session) {
        if (session !== active || !active.cancellation.canAct()) return
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
        val terminalStatus = if (active.cancellation.canAct()) status else "FAILED"
        val terminalStage = active.cancellation.stage() ?: stage
        active.phase = Phase.Finishing
        UnlockGateActivity.closeGate()
        val currentlyLocked = getSystemService(KeyguardManager::class.java).isDeviceLocked
        val currentForegroundPackage = runCatching(::foregroundPackage).getOrNull()
        val shouldRequestHome = if (active.isImageCapture) {
            !active.startedLocked && terminalStatus == "IMAGE_CAPTURED" && currentForegroundPackage == NotificationSnapshot.WechatPackage
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
        if (status == "SENT_TO_WECHAT" && finalized) {
            if (active.startedLocked) successfulLockedRunAwaitingAck = true
            active.result = AccessibilityReplyResult(status, stage, startedLocked = active.startedLocked)
        } else if (status == "IMAGE_CAPTURED" && finalized && active.capturedMedia != null) {
            active.result = AccessibilityReplyResult(status, stage, active.capturedMedia, active.startedLocked)
        } else {
            active.capturedMedia?.bytes?.fill(0)
            active.result = AccessibilityReplyResult(
                if (status == "SENT_TO_WECHAT" || status == "IMAGE_CAPTURED") "FAILED" else status,
                if (status == "SENT_TO_WECHAT" || status == "IMAGE_CAPTURED") "FINALIZATION_FAILED" else stage,
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
        private const val WechatChatTopPx = 284
        private const val TitleRegionFraction = 0.45f
        private val SendLabels = setOf("发送", "Send")
        private val SearchLabels = setOf("搜索", "Search")
        private val ImageExecutor = Executors.newSingleThreadExecutor { runnable -> Thread(runnable, "relay-image-capture") }

        @Volatile private var liveService: LockscreenAccessibilityReplyService? = null
        @Volatile private var successfulLockedRunAwaitingAck = false

        fun isLive(): Boolean = liveService != null

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
