package com.aurora.wechatrelay.probe

import android.app.PendingIntent
import android.app.RemoteInput
import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.os.UserManager
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import org.json.JSONObject
import java.util.concurrent.Executors
import java.util.concurrent.ExecutorService
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

class WechatNotificationListenerService : NotificationListenerService() {
    private val duplicateDetector = DuplicateDetector()
    // ponytail: WeChat has no message ID here; only merge identical reposts within 250ms, use a message ID if available.
    private val voiceDuplicateDetector = DuplicateDetector(250L)
    private val syncExecutor = Executors.newSingleThreadExecutor()
    private val historyExecutor = Executors.newSingleThreadExecutor()
    private val historyDrainScheduled = AtomicBoolean(false)
    private val historyRecovered = AtomicBoolean(false)
    private data class HistorySource(val sourceKey: String, val postTime: Long, val contentIntent: PendingIntent?, val kind: String = "history")
    private val historySources = ConcurrentHashMap<String, HistorySource>()
    @Volatile private var replyExecutor: ExecutorService? = null
    @Volatile private var replyGeneration = 0L
    // ponytail: one newest notification cancels stale capture; per-conversation scheduling if throughput requires it.
    @Volatile private var latestCaptureIdentity: String? = null
    private var lastSyncDuplicateKey: String? = null
    private var lastSyncPreviewHash: String? = null
    private var lastSyncDuplicateMillis = 0L

    override fun onListenerConnected() {
        ProbeRuntime.listenerConnected = true
        scanRemoteInputCandidates()
        startReplyPolling()
        syncExecutor.execute {
            val store = SyncStore.get(this)
            if (store.paired()) {
                if (historyRecovered.compareAndSet(false, true)) store.recoverHistoryTasks(store.deviceId())
                scheduleHistoryDrain()
            }
        }
        // Recover an active notification that the listener missed while its binding was unavailable.
        activeNotifications?.filter(::isSourceWechat)?.forEach(::onNotificationPosted)
        try {
            syncExecutor.execute {
                try {
                    when (SyncNetwork.uploadPending(this)) {
                        SyncNetwork.UploadResult.Retry -> SyncNetwork.enqueue(this)
                        SyncNetwork.UploadResult.Complete -> Unit
                    }
                } catch (failure: Exception) {
                    recordSyncDiagnostic("RECOVER_PENDING_FAILED", failure)
                }
            }
        } catch (failure: Exception) {
            recordSyncDiagnostic("RECOVER_PENDING_FAILED", failure)
        }
    }

    override fun onListenerDisconnected() {
        ProbeRuntime.listenerConnected = false
        stopReplyPolling()
    }

    override fun onNotificationPosted(sbn: StatusBarNotification) {
        if (!isSourceWechat(sbn)) return
        startReplyPolling()

        try {
            val captured = NotificationSnapshot.capture(this, sbn)
            val duplicateCandidate = duplicateDetector.isDuplicateCandidate(captured.fingerprint, System.currentTimeMillis())
            captured.json.put("duplicateCandidate", duplicateCandidate)
            ProbeStore(this).append(captured.json.toString())
            ProbeRuntime.lastCaptureMillis = System.currentTimeMillis()
            ProbeRuntime.remoteInputCandidates = captured.remoteInputCandidates
            if (!duplicateCandidate && SyncStore.get(this).paired()) {
                val preview = NotificationSnapshot.preview(sbn)
                    ?: return recordSyncDiagnostic("PREVIEW_NULL")
                val notificationIdentity = notificationIdentity(sbn, preview)
                val historyNotification = ChatHistoryForwardPolicy.shouldForward(
                    preview, NotificationSnapshot.conversationTitle(sbn),
                    sbn.notification.flags and android.app.Notification.FLAG_GROUP_SUMMARY != 0,
                    System.currentTimeMillis() - sbn.postTime,
                )
                val voiceNotification = VoiceTranscriptionPolicy.shouldTranscribe(preview, NotificationSnapshot.conversationTitle(sbn),
                    sbn.notification.flags and android.app.Notification.FLAG_GROUP_SUMMARY != 0, System.currentTimeMillis() - sbn.postTime)
                if (voiceNotification && voiceDuplicateDetector.isDuplicateCandidate(
                        SyncProtocol.shortWindowDuplicateKey(sbn.key, sbn.userId, preview), android.os.SystemClock.elapsedRealtime(),
                    )) {
                    recordSyncDiagnostic("VOICE_NOTIFICATION_REPOST_IGNORED")
                    return
                }
                latestCaptureIdentity = notificationIdentity
                val imageNotification = ImageNotificationPolicy.shouldCapture(preview, sbn.notification.flags and android.app.Notification.FLAG_GROUP_SUMMARY != 0, System.currentTimeMillis() - sbn.postTime)
                if (!imageNotification && !historyNotification && !voiceNotification && isSyncDuplicate(sbn.key, sbn.userId, preview)) return
                val store = SyncStore.get(this)
                val expectedDeviceId = store.deviceId()
                if (store.hasQueuedNotificationIdentity(expectedDeviceId, notificationIdentity)) return
                if (historyNotification || voiceNotification) historySources[notificationIdentity] = HistorySource(sbn.key, sbn.postTime, sbn.notification.contentIntent, if (voiceNotification) "voice" else "history")
                try {
                    syncExecutor.execute {
                        try {
                            if (!store.hasQueuedNotificationIdentity(expectedDeviceId, notificationIdentity)) {
                                queuePreview(store, expectedDeviceId, preview, notificationIdentity, sbn, captured.remoteInputCandidates.firstOrNull())
                            }
                        } catch (failure: Exception) {
                            historySources.remove(notificationIdentity)
                            recordSyncDiagnostic("QUEUE_PREVIEW_FAILED", failure)
                        }
                    }
                } catch (failure: Exception) {
                    recordSyncDiagnostic("EXECUTOR_REJECTED", failure)
                }
            }
        } catch (failure: Exception) {
            recordSyncDiagnostic("OUTER_CAPTURE_FAILED", failure)
        }
    }

    private fun queuePreview(store: SyncStore, expectedDeviceId: String, preview: Preview, notificationIdentity: String, sbn: StatusBarNotification, replyCandidate: RemoteInputCandidate?) {
        val capturedMedia = NotificationMedia.capture(this, sbn).toMutableList()
        var imageResult: AccessibilityReplyResult? = null
        if (capturedMedia.none { it.kind == "image" || it.kind == "sticker" } &&
            ImageNotificationPolicy.shouldCapture(preview, sbn.notification.flags and android.app.Notification.FLAG_GROUP_SUMMARY != 0, System.currentTimeMillis() - sbn.postTime) &&
            sbn.notification.contentIntent?.creatorPackage == NotificationSnapshot.WechatPackage &&
            latestCaptureIdentity == notificationIdentity && store.isCurrentDevice(expectedDeviceId)
        ) {
            imageResult = runCatching { LockscreenAccessibilityReplyService.executeImage(
                sbn.notification.contentIntent,
                NotificationSnapshot.conversationTitleHash(this, sbn),
                preview.sender, sbn.userId,
                getSystemService(UserManager::class.java).getSerialNumberForUser(sbn.user),
            ) { latestCaptureIdentity == notificationIdentity && store.isCurrentDevice(expectedDeviceId) }
            }.getOrElse { AccessibilityReplyResult("FAILED", "CAPTURE_EXCEPTION") }
            recordSyncDiagnostic("IMAGE_${imageResult.stage}")
            imageResult.media?.let {
                if (latestCaptureIdentity == notificationIdentity && store.isCurrentDevice(expectedDeviceId)) capturedMedia.add(it)
                else it.bytes.fill(0)
            }
        }
        try {
            val id = SyncProtocol.uuidV7()
            val createdAt = System.currentTimeMillis()
            val encryption = try {
                store.messageEncryptionContext(expectedDeviceId)
            } catch (failure: Exception) {
                recordSyncDiagnostic("KEY_UNWRAP_FAILED", failure)
                return
            }
            val seq = try {
                store.nextSeq(expectedDeviceId)
            } catch (failure: Exception) {
                encryption.a2iKey.fill(0)
                recordSyncDiagnostic("NEXT_SEQ_FAILED", failure)
                return
            }
            val envelope: Envelope
            val assetsJson: String
            try {
                envelope = SyncProtocol.encrypt(encryption.a2iKey, id, encryption.deviceId, seq, createdAt, preview, sbn.userId)
                assetsJson = try {
                    capturedMedia.take(2).map { media ->
                        val assetId = SyncProtocol.uuidV7()
                        SyncProtocol.assetJson(MediaAsset(assetId, media.kind, media.mimeType, media.width, media.height, SyncProtocol.encryptAsset(encryption.a2iKey, assetId, encryption.deviceId, seq, createdAt, media.bytes, sbn.userId)))
                    }.joinToString(prefix = "[", postfix = "]")
                } catch (failure: Exception) {
                    // Media is best-effort; encrypted text delivery is the durable path.
                    recordSyncDiagnostic("MEDIA_ENCRYPT_FAILED", failure)
                    "[]"
                }
            } catch (failure: Exception) {
                recordSyncDiagnostic("ENCRYPT_FAILED", failure)
                return
            } finally {
                encryption.a2iKey.fill(0)
            }
            val titleHash = runCatching { NotificationSnapshot.conversationTitleHash(this, sbn) }.getOrDefault("")
            val wechatUserSerial = runCatching { getSystemService(UserManager::class.java).getSerialNumberForUser(sbn.user) }.getOrDefault(-1L)
            val accessibilityArmed = runCatching { LockscreenPinStore(this).status(
                    accessibilityLive = LockscreenAccessibilityReplyService.isLive(),
                    userUnlocked = getSystemService(UserManager::class.java).isUserUnlocked,
                ).armed }.getOrDefault(false)
            val replyTarget = try {
                when (ReplyRoutes.select(replyCandidate != null, accessibilityArmed, NotificationSnapshot.hasWechatContentIntent(sbn), titleHash)) {
                    ReplyRoutes.RemoteInput -> replyCandidate?.let {
                        ReplyTarget(id, it.sbnKey, sbn.postTime, it.actionIndex, createdAt, it.wechatUserId, wechatUserSerial, ReplyRoutes.RemoteInput, titleHash)
                    }
                    ReplyRoutes.Accessibility -> ReplyTarget(id, sbn.key, sbn.postTime, -1, createdAt, sbn.userId, wechatUserSerial, ReplyRoutes.Accessibility, titleHash)
                    else -> null
                }
            } catch (_: Exception) {
                null
            }
            val conversationSendCapable = replyTarget != null && accessibilityArmed &&
                NotificationSnapshot.isConversationSendCandidate(sbn) && titleHash.isNotBlank() && wechatUserSerial >= 0L
            val queued = try {
                // Share the persisted UI queue so voice and history operations cannot race for WeChat.
                val historyForward = if (historySources.containsKey(notificationIdentity)) {
                    HistoryForwardInput(notificationIdentity, sbn.key, wechatUserSerial, sbn.postTime,
                        JSONObject().put("sender", preview.sender).put("conversationTitle", NotificationSnapshot.conversationTitle(sbn))
                            .put("body", preview.body).put("kind", historySources.getValue(notificationIdentity).kind)
                            .put("knownTexts", org.json.JSONArray()).toString())
                } else null
                store.enqueue(expectedDeviceId, id, seq, createdAt, SyncProtocol.envelopeJson(envelope), assetsJson, sbn.userId, replyTarget, conversationSendCapable, historyForward)
            } catch (failure: Exception) {
                recordSyncDiagnostic("DB_ENQUEUE_FAILED", failure)
                return
            }
            if (!queued) return recordSyncDiagnostic("DB_ENQUEUE_REJECTED")
            if (imageResult?.status == "IMAGE_CAPTURED" && imageResult.startedLocked && assetsJson != "[]") {
                if (!LockscreenPinStore(this).completeSuccessfulFinalization()) recordSyncDiagnostic("IMAGE_ATTEMPT_FINALIZE_FAILED")
            }
            try {
                store.markQueuedNotificationIdentity(expectedDeviceId, notificationIdentity)
            } catch (failure: Exception) {
                recordSyncDiagnostic("PERSISTED_IDENTITY_FAILED", failure)
                return
            }
            if (historySources.containsKey(notificationIdentity)) scheduleHistoryDrain()
            else store.noteHistoryText(expectedDeviceId, sbn.key, sbn.postTime, ChatHistoryForwardPolicy.notificationBody(preview))
            when (SyncNetwork.uploadPending(this, expectedDeviceId)) {
                SyncNetwork.UploadResult.Retry -> try {
                    SyncNetwork.enqueue(this)
                    } catch (failure: Exception) {
                        recordSyncDiagnostic("WORKMANAGER_ENQUEUE_FAILED", failure)
                    }
                SyncNetwork.UploadResult.Complete -> Unit
            }
        } finally {
            capturedMedia.forEach { it.bytes.fill(0) }
        }
    }

    private fun scheduleHistoryDrain() {
        if (!historyDrainScheduled.compareAndSet(false, true)) return
        try {
            historyExecutor.execute {
                val store = SyncStore.get(this)
                var expectedDeviceId: String? = null
                var activeTask: HistoryForwardTask? = null
                try {
                    if (!store.paired()) return@execute
                    val deviceId = store.deviceId()
                    expectedDeviceId = deviceId
                    // Let immediately following notifications reach the encrypted text queue before opening WeChat.
                    Thread.sleep(1_000)
                    while (!Thread.currentThread().isInterrupted && store.isCurrentDevice(deviceId)) {
                        val tasks = store.pendingHistoryTasks(deviceId)
                        tasks.forEach {
                            val kind = JSONObject(store.historyTaskPayload(deviceId, it)).optString("kind", "history")
                            historySources.putIfAbsent(it.id, HistorySource(it.sourceKey, it.postTime, null, kind))
                        }
                        val task = tasks.firstOrNull() ?: break
                        activeTask = task
                        val payload = JSONObject(store.historyTaskPayload(deviceId, task))
                        val voice = payload.optString("kind", "history") == "voice"
                        val title = payload.getString("conversationTitle")
                        val sender = payload.getString("sender")
                        val freshnessTime = payload.optLong("manualRetryAt", task.postTime)
                        store.historyTaskState(deviceId, task.id, SyncStore.HistoryRunning, "OPENING")
                        val cachedResult = payload.optJSONObject("voiceResult")
                        if (voice && cachedResult != null) {
                            val completed = queueVoiceResult(task, title, sender, AccessibilityReplyResult(
                                cachedResult.getString("status"), cachedResult.getString("stage"),
                                transcript = cachedResult.optString("transcript").takeIf(String::isNotBlank)))
                            historySources.remove(task.id)
                            activeTask = null
                            if (!completed) { Thread.sleep(10_000); break }
                            continue
                        }
                        if (System.currentTimeMillis() - freshnessTime !in 0..120_000) {
                            val completed = if (voice) queueVoiceResult(task, title, sender, AccessibilityReplyResult("FAILED", "SOURCE_EXPIRED"))
                            else { store.historyTaskState(deviceId, task.id, SyncStore.HistoryFailed, "SOURCE_EXPIRED"); true }
                            historySources.remove(task.id)
                            activeTask = null
                            if (!completed) { Thread.sleep(10_000); break }
                            continue
                        }
                        val contentIntent = historySources[task.id]?.contentIntent?.takeIf { it.creatorPackage == NotificationSnapshot.WechatPackage }
                        val isCurrent = { ProbeRuntime.listenerConnected && store.isCurrentDevice(deviceId) && System.currentTimeMillis() - freshnessTime in 0..120_000 &&
                            historySources.none { (id, source) -> id != task.id && source.sourceKey == task.sourceKey && source.postTime == task.postTime } }
                        val cardsAfter = { historySources.values.count { it.sourceKey == task.sourceKey && it.kind == (if (voice) "voice" else "history") && it.postTime > task.postTime } }
                        val deadline = android.os.SystemClock.uptimeMillis() + 30_000
                        var result: AccessibilityReplyResult
                        do {
                            result = if (voice) LockscreenAccessibilityReplyService.executeVoiceTranscription(
                                contentIntent, Privacy.saltedHash(title, Privacy.salt(this)), title, sender,
                                task.wechatUserId, task.wechatUserSerial, isCurrent, cardsAfter,
                                { texts -> queueRecoveredHistoryTexts(task, title, sender, texts) },
                                VoiceTranscriptionPolicy.notificationDuration(Preview(sender, payload.getString("body"), "text")),
                            ) else LockscreenAccessibilityReplyService.executeHistoryForward(
                                contentIntent, Privacy.saltedHash(title, Privacy.salt(this)), title, sender,
                                task.wechatUserId, task.wechatUserSerial, isCurrent,
                                { store.claimHistorySend(deviceId, task.id) }, cardsAfter,
                                { texts -> queueRecoveredHistoryTexts(task, title, sender, texts) },
                            )
                            if (result.stage != "ACCESSIBILITY_BUSY" || !isCurrent() || android.os.SystemClock.uptimeMillis() >= deadline) break
                            Thread.sleep(250)
                        } while (isCurrent())
                        val sending = store.findHistoryTask(deviceId, task.id)?.state == SyncStore.HistorySending
                        val state = if (result.status == "HISTORY_FORWARDED") SyncStore.HistorySent
                            else if (sending) SyncStore.HistoryUnknown else SyncStore.HistoryFailed
                        val completed = if (voice) queueVoiceResult(task, title, sender, result)
                        else { store.historyTaskState(deviceId, task.id, state, result.stage); true }
                        historySources.remove(task.id)
                        activeTask = null
                        recordSyncDiagnostic("${if (voice) "VOICE" else "HISTORY"}_${result.stage}")
                        if (!completed) { Thread.sleep(10_000); break }
                    }
                } catch (failure: InterruptedException) {
                    Thread.currentThread().interrupt()
                } catch (failure: Exception) {
                    recordSyncDiagnostic("HISTORY_FORWARD_FAILED", failure)
                } finally {
                    activeTask?.let { task ->
                        if (store.isCurrentDevice(task.deviceId)) {
                            val state = store.findHistoryTask(task.deviceId, task.id)?.state
                            if (state in setOf(SyncStore.HistoryRunning, SyncStore.HistorySending)) {
                                store.historyTaskState(task.deviceId, task.id, if (state == SyncStore.HistorySending) SyncStore.HistoryUnknown else SyncStore.HistoryFailed, "WORKFLOW_INTERRUPTED")
                            }
                        }
                        historySources.remove(task.id)
                    }
                    historyDrainScheduled.set(false)
                    expectedDeviceId?.takeIf(store::isCurrentDevice)?.let { deviceId ->
                        ProbeRuntime.historyQueueStatus = store.historyTaskCounts(deviceId).entries.joinToString { "${it.key}=${it.value}" }.ifEmpty { "空" }
                        if (!historyExecutor.isShutdown && store.pendingHistoryTasks(deviceId).isNotEmpty()) scheduleHistoryDrain()
                    }
                }
            }
        } catch (failure: Exception) {
            historyDrainScheduled.set(false)
            recordSyncDiagnostic("HISTORY_EXECUTOR_UNAVAILABLE", failure)
        }
    }

    private fun queueVoiceResult(task: HistoryForwardTask, title: String, sender: String, result: AccessibilityReplyResult): Boolean {
        // Wait on the existing text executor so trailing-text recovery and result completion remain ordered.
        return syncExecutor.submit<Boolean> {
            val store = SyncStore.get(this)
            if (!store.isCurrentDevice(task.deviceId)) return@submit true
            store.cacheVoiceResult(task, result)
            val succeeded = result.status == "VOICE_TRANSCRIBED" && !result.transcript.isNullOrBlank()
            val previews = VoiceResultMessages.previews(sender, task.postTime, result.transcript.takeIf { succeeded }, result.stage)
            val entries = previews.map { preview ->
                val id = SyncProtocol.uuidV7()
                val seq = store.nextSeq(task.deviceId)
                val createdAt = System.currentTimeMillis()
                val encryption = store.messageEncryptionContext(task.deviceId)
                val envelope = try { SyncProtocol.encrypt(encryption.a2iKey, id, task.deviceId, seq, createdAt, preview, task.wechatUserId) }
                    finally { encryption.a2iKey.fill(0) }
                val target = ReplyTarget(id, task.sourceKey, task.postTime, -1, createdAt, task.wechatUserId, task.wechatUserSerial,
                    ReplyRoutes.Accessibility, Privacy.saltedHash(title, Privacy.salt(this)))
                SyncQueueItem(id, task.deviceId, seq, createdAt, SyncProtocol.envelopeJson(envelope), "[]", task.wechatUserId, true, true, SyncStore.Queued) to target
            }
            if (!store.completeVoiceTask(task, entries, succeeded, result.stage)) {
                recordSyncDiagnostic("VOICE_RESULT_QUEUE_FULL")
                SyncNetwork.enqueue(this)
                return@submit false
            }
            recordSyncDiagnostic(if (succeeded) "VOICE_TRANSCRIPT_QUEUED" else "VOICE_FAILURE_QUEUED")
            try {
                if (SyncNetwork.uploadPending(this, task.deviceId) == SyncNetwork.UploadResult.Retry) SyncNetwork.enqueue(this)
                else if (store.paired() && store.isCurrentDevice(task.deviceId)) recordSyncDiagnostic(if (succeeded) "VOICE_TRANSCRIPT_SERVER_ACCEPTED" else "VOICE_FAILURE_SERVER_ACCEPTED")
            } catch (failure: Exception) {
                recordSyncDiagnostic("VOICE_UPLOAD_RETRY", failure)
                SyncNetwork.enqueue(this)
            }
            true
        }.get()
    }

    private fun queueRecoveredHistoryTexts(task: HistoryForwardTask, title: String, sender: String, texts: List<String>) {
        if (texts.isEmpty()) return
        syncExecutor.execute {
            val store = SyncStore.get(this)
            try {
                if (!store.isCurrentDevice(task.deviceId)) return@execute
                val current = store.findHistoryTask(task.deviceId, task.id) ?: return@execute
                val known = JSONObject(store.historyTaskPayload(task.deviceId, current)).optJSONArray("knownTexts")
                val notified = (0 until (known?.length() ?: 0)).map { requireNotNull(known).getString(it) }
                for ((index, text) in ChatHistoryForwardPolicy.missingTexts(texts, notified)) {
                    val preview = SyncProtocol.normalizePreview(sender, text, "text")
                    val id = SyncProtocol.uuidV7()
                    val seq = store.nextSeq(task.deviceId)
                    val createdAt = System.currentTimeMillis()
                    val encryption = store.messageEncryptionContext(task.deviceId)
                    val envelope = try { SyncProtocol.encrypt(encryption.a2iKey, id, task.deviceId, seq, createdAt, preview, task.wechatUserId) }
                        finally { encryption.a2iKey.fill(0) }
                    val target = ReplyTarget(id, task.sourceKey, task.postTime, -1, createdAt, task.wechatUserId, task.wechatUserSerial,
                        ReplyRoutes.Accessibility, Privacy.saltedHash(title, Privacy.salt(this)))
                    if (!store.enqueue(task.deviceId, id, seq, createdAt, SyncProtocol.envelopeJson(envelope), wechatUserId = task.wechatUserId,
                            replyTarget = target, conversationSendCapable = true, historyRecovery = task.id to text)) {
                        recordSyncDiagnostic("HISTORY_TEXT_QUEUE_REJECTED")
                        break
                    }
                    recordSyncDiagnostic("HISTORY_TEXT_RECOVERED_$index")
                }
                if (SyncNetwork.uploadPending(this, task.deviceId) == SyncNetwork.UploadResult.Retry) SyncNetwork.enqueue(this)
            } catch (failure: Exception) {
                recordSyncDiagnostic("HISTORY_TEXT_RECOVERY_FAILED", failure)
            }
        }
    }

    private fun recordSyncDiagnostic(stage: String, failure: Exception? = null) {
        if (stage.startsWith("HISTORY_") || stage.startsWith("VOICE_")) ProbeRuntime.lastDiagnostic = stage
        try {
            val event = JSONObject()
                .put("eventType", "syncDiagnostic")
                .put("capturedAt", System.currentTimeMillis())
                .put("stage", stage)
            if (failure != null) event.put("exceptionClass", failure.javaClass.name)
            failure?.let(::safeFailureDetail)?.let {
                event.put("detail", it)
                ProbeRuntime.lastDiagnostic = "$stage: $it"
            }
            ProbeStore(this).append(event.toString())
        } catch (_: Exception) {
        }
    }

    @Synchronized
    private fun isSyncDuplicate(notificationKey: String, wechatUserId: Int, preview: Preview): Boolean {
        val now = System.currentTimeMillis()
        val duplicateKey = SyncProtocol.shortWindowDuplicateKey(notificationKey, wechatUserId, preview)
        val previewHash = Privacy.saltedHash(duplicateKey, Privacy.salt(this))
        val duplicate = duplicateKey == lastSyncDuplicateKey &&
            previewHash == lastSyncPreviewHash &&
            now - lastSyncDuplicateMillis <= SyncDuplicateWindowMillis
        if (!duplicate) {
            lastSyncDuplicateKey = duplicateKey
            lastSyncPreviewHash = previewHash
            lastSyncDuplicateMillis = now
        }
        return duplicate
    }

    private fun notificationIdentity(sbn: StatusBarNotification, preview: Preview): String = Privacy.saltedHash(
        SyncProtocol.notificationIdentity(sbn.key, sbn.userId, sbn.postTime, preview),
        Privacy.salt(this),
    )

    @Suppress("DEPRECATION")
    private fun isSourceWechat(sbn: StatusBarNotification): Boolean =
        NotificationSnapshot.isAllowedWechatSource(sbn.packageName, sbn.userId, sbn.notification.channelId)

    override fun onDestroy() {
        ProbeRuntime.listenerConnected = false
        stopReplyPolling()
        syncExecutor.shutdown()
        historySources.clear()
        historyExecutor.shutdownNow()
        super.onDestroy()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ActionScanCandidates -> scanRemoteInputCandidates()
            ActionStartReplyPolling -> startReplyPolling()
            ActionReply -> ProbeRuntime.takeOneTimeReply()?.let(::executeOneTimeReply)
            ActionRetryVoice -> syncExecutor.execute {
                val store = SyncStore.get(this)
                if (store.paired() && store.retryLatestFailedVoice(store.deviceId())) scheduleHistoryDrain()
            }
        }
        stopSelf(startId)
        return START_NOT_STICKY
    }

    private fun scanRemoteInputCandidates() {
        ProbeRuntime.remoteInputCandidates = activeNotifications
            ?.asSequence()
            ?.filter(::isSourceWechat)
            ?.flatMap { NotificationSnapshot.capture(this, it).remoteInputCandidates.asSequence() }
            ?.toList()
            .orEmpty()
    }

    private fun executeOneTimeReply(request: InMemoryReplyRequest) {
        val candidate = request.candidate
        val sbnKey = candidate.sbnKey
        val wechatUserId = candidate.wechatUserId
        val actionIndex = candidate.actionIndex
        try {
            val sbn = activeNotifications?.firstOrNull {
                isSourceWechat(it) && it.key == sbnKey && it.userId == wechatUserId
            }
            if (sbn == null) return recordReply("NOTIFICATION_NOT_ACTIVE", sbnKey, actionIndex)
            val action = sbn.notification.actions?.getOrNull(actionIndex)
                ?: return recordReply("WECHAT_ACTION_CHANGED", sbnKey, actionIndex)
            val pendingIntent = action.actionIntent
                ?: return recordReply("WECHAT_ACTION_CHANGED", sbnKey, actionIndex)
            val remoteInputs = action.remoteInputs?.filter { it.allowFreeFormInput }.orEmpty()
            if (pendingIntent.creatorPackage != NotificationSnapshot.WechatPackage || remoteInputs.isEmpty()) {
                return recordReply("REMOTE_INPUT_UNSUPPORTED", sbnKey, actionIndex)
            }
            val fillInIntent = Intent()
            val results = Bundle().apply {
                remoteInputs.forEach { putCharSequence(it.resultKey, request.replyText.concatToString()) }
            }
            RemoteInput.addResultsToIntent(remoteInputs.toTypedArray(), fillInIntent, results)
            pendingIntent.send(this, 0, fillInIntent)
            recordReply("SENT_TO_WECHAT", sbnKey, actionIndex)
        } catch (_: PendingIntent.CanceledException) {
            recordReply("PENDING_INTENT_CANCELED", sbnKey, actionIndex)
        } catch (_: RuntimeException) {
            recordReply("FAILED", sbnKey, actionIndex)
        } finally {
            request.replyText.fill('\u0000')
        }
    }

    private fun startReplyPolling() {
        synchronized(this) {
            if (!ProbeRuntime.listenerConnected) {
                ProbeRuntime.lastReplyStatus = "等待通知监听连接"
                return
            }
            if (!SyncStore.get(this).paired()) {
                ProbeRuntime.lastReplyStatus = "尚未完成配对"
                return
            }
            if (replyExecutor != null) return
            val generation = replyGeneration + 1L
            replyGeneration = generation
            val executor = Executors.newSingleThreadExecutor()
            replyExecutor = executor
            ProbeRuntime.lastReplyStatus = "轮询运行中"
            ProbeRuntime.lastDiagnostic = "暂无异常"
            recordSyncDiagnostic("REPLY_POLL_STARTED")
            val expectedDeviceId = SyncStore.get(this).deviceId()
            executor.execute { pollReplies(generation, expectedDeviceId) }
        }
    }

    private fun stopReplyPolling() {
        synchronized(this) {
            replyGeneration += 1L
            replyExecutor?.shutdownNow()
        }
    }

    private fun pollReplies(generation: Long, expectedDeviceId: String) {
        try {
            while (replySessionActive(generation, expectedDeviceId)) {
                try {
                    flushPendingReplyAck(expectedDeviceId)
                    val command = SyncNetwork.pollReply(this, expectedDeviceId)
                    // A command belongs to the device generation that authenticated this long poll.
                    if (command != null && replySessionActive(generation, expectedDeviceId)) {
                        recordSyncDiagnostic("REPLY_COMMAND_RECEIVED")
                        processReplyCommand(command, expectedDeviceId)
                    }
                } catch (failure: Exception) {
                    if (replySessionActive(generation, expectedDeviceId)) {
                        ProbeRuntime.lastReplyStatus = "轮询失败: ${safeFailureDetail(failure)}"
                        recordSyncDiagnostic("REPLY_POLL_FAILED", failure)
                        try {
                            Thread.sleep(ReplyPollFailureBackoffMillis)
                        } catch (_: InterruptedException) {
                            Thread.currentThread().interrupt()
                        }
                    }
                }
            }
        } finally {
            val restart = synchronized(this) {
                replyExecutor = null
                ProbeRuntime.listenerConnected && SyncStore.get(this).paired()
            }
            if (restart) startReplyPolling()
        }
    }

    private fun processReplyCommand(command: ReplyCommand, expectedDeviceId: String) {
        val store = SyncStore.get(this)
        if (command.deviceId != expectedDeviceId || !store.isCurrentDevice(expectedDeviceId)) return
        // UI automation is intentionally outside every SyncStore synchronized call.
        val status = try {
            executeReplyCommand(command, store, expectedDeviceId)
        } catch (_: Exception) {
            "FAILED"
        }
        if (!store.isCurrentDevice(expectedDeviceId)) return
        store.savePendingReplyAck(expectedDeviceId, command.id, status)
        if (status == "SENT_TO_WECHAT" && LockscreenAccessibilityReplyService.consumeSuccessfulLockedRun()) {
            // Clear the attempt marker only after the at-most-once ACK is durable.
            if (!LockscreenPinStore(this).completeSuccessfulFinalization()) recordSyncDiagnostic("ACCESSIBILITY_ATTEMPT_FINALIZE_FAILED")
        }
        ProbeRuntime.lastReplyStatus = replyStatusLabel(status)
        try {
            flushPendingReplyAck(expectedDeviceId)
            recordSyncDiagnostic("REPLY_ACK_SENT")
        } catch (failure: Exception) {
            recordSyncDiagnostic("REPLY_ACK_FAILED", failure)
        }
    }

    private fun flushPendingReplyAck(expectedDeviceId: String) {
        val store = SyncStore.get(this)
        val pending = store.pendingReplyAck(expectedDeviceId) ?: return
        SyncNetwork.acknowledgeReply(this, expectedDeviceId, pending.replyId, pending.status)
        store.clearPendingReplyAck(expectedDeviceId, pending.replyId)
    }

    private fun executeReplyCommand(command: ReplyCommand, store: SyncStore, expectedDeviceId: String): String {
        if (!ProbeRuntime.listenerConnected) return "NOTIFICATION_NOT_ACTIVE"
        val decrypted = try {
            val key = store.i2aKey(expectedDeviceId)
            try {
                if (command.v == 3) {
                    val pairId = command.pairId ?: return "INVALID_REPLY"
                    SyncProtocol.decryptI2aConversationSend(key, pairId, command.id, command.deviceId, command.targetMessageId, command.createdAt, command.replyEnvelope, command.wechatUserId)
                } else {
                    DecryptedReply(
                        SyncProtocol.decryptI2aReply(key, command.id, command.deviceId, command.targetMessageId, command.createdAt, command.replyEnvelope, if (command.v == 2) command.wechatUserId else null),
                        null,
                    )
                }
            } finally {
                key.fill(0)
            }
        } catch (_: Exception) {
            return "INVALID_REPLY"
        }
        val target = store.replyTarget(command.targetMessageId) ?: return "NOTIFICATION_NOT_ACTIVE"
        if (target.wechatUserId != command.wechatUserId) return "INVALID_REPLY"
        val conversationTitle = decrypted.conversationTitle
        if (command.v == 3 && (conversationTitle == null || !LockscreenReplySelectors.plaintextTitleMatches(
                target.conversationTitleHash,
                conversationTitle,
                Privacy.salt(this),
            ))) return "INVALID_REPLY"
        val sbn = activeNotifications?.firstOrNull { isSourceWechat(it) && it.key == target.sbnKey && it.userId == target.wechatUserId }
        if (sbn == null) {
            if (command.v != 3 || conversationTitle == null || target.conversationTitleHash.isBlank()) return "NOTIFICATION_NOT_ACTIVE"
            val result = LockscreenAccessibilityReplyService.executeConversationSend(
                null,
                target.conversationTitleHash,
                conversationTitle,
                target.wechatUserId,
                target.wechatUserSerial,
                decrypted.body,
            )
            recordSyncDiagnostic("ACCESSIBILITY_${result.stage}")
            if (result.status == "SENT_TO_WECHAT") recordReply(result.status, target.sbnKey, target.actionIndex)
            return result.status
        }
        val currentTitleHash = NotificationSnapshot.conversationTitleHash(this, sbn)
        if (target.conversationTitleHash.isNotBlank() && !NotificationSnapshot.targetMatches(
                target.sbnKey,
                target.wechatUserId,
                target.conversationTitleHash,
                sbn.key,
                sbn.userId,
                currentTitleHash,
            )
        ) return "WECHAT_ACTION_CHANGED"
        if (target.route == ReplyRoutes.Accessibility) {
            if (target.conversationTitleHash.isBlank()) return "WECHAT_ACTION_CHANGED"
            val contentIntent = sbn.notification.contentIntent
                ?.takeIf { it.creatorPackage == NotificationSnapshot.WechatPackage }
                ?: return "WECHAT_ACTION_CHANGED"
            val result = if (command.v == 3 && conversationTitle != null) {
                LockscreenAccessibilityReplyService.executeConversationSend(contentIntent, target.conversationTitleHash, conversationTitle, target.wechatUserId, target.wechatUserSerial, decrypted.body)
            } else {
                LockscreenAccessibilityReplyService.execute(contentIntent, target.conversationTitleHash, decrypted.body)
            }
            recordSyncDiagnostic("ACCESSIBILITY_${result.stage}")
            if (result.status == "SENT_TO_WECHAT") recordReply(result.status, target.sbnKey, target.actionIndex)
            return result.status
        }
        if (target.route != ReplyRoutes.RemoteInput) return "INVALID_REPLY"
        val action = sbn.notification.actions?.getOrNull(target.actionIndex)
            ?: return "WECHAT_ACTION_CHANGED"
        val pendingIntent = action.actionIntent ?: return "WECHAT_ACTION_CHANGED"
        val remoteInputs = action.remoteInputs?.filter { it.allowFreeFormInput }.orEmpty()
        if (pendingIntent.creatorPackage != NotificationSnapshot.WechatPackage || remoteInputs.isEmpty()) {
            return "REMOTE_INPUT_UNSUPPORTED"
        }
        val fillInIntent = Intent()
        val results = Bundle().apply {
            remoteInputs.forEach { putCharSequence(it.resultKey, decrypted.body) }
        }
        RemoteInput.addResultsToIntent(remoteInputs.toTypedArray(), fillInIntent, results)
        return try {
            pendingIntent.send(this, 0, fillInIntent)
            recordReply("SENT_TO_WECHAT", target.sbnKey, target.actionIndex)
            "SENT_TO_WECHAT"
        } catch (_: PendingIntent.CanceledException) {
            recordReply("PENDING_INTENT_CANCELED", target.sbnKey, target.actionIndex)
            "PENDING_INTENT_CANCELED"
        } catch (_: RuntimeException) {
            recordReply("FAILED", target.sbnKey, target.actionIndex)
            "FAILED"
        }
    }

    private fun replySessionActive(generation: Long, expectedDeviceId: String): Boolean =
        ProbeRuntime.listenerConnected && replyGeneration == generation && SyncStore.get(this).isCurrentDevice(expectedDeviceId) && !Thread.currentThread().isInterrupted

    private fun safeFailureDetail(failure: Exception): String = when (failure) {
        is java.net.SocketTimeoutException -> "网络读取超时"
        is java.net.UnknownHostException -> "DNS解析失败"
        is java.net.ConnectException -> "无法连接服务器"
        is javax.net.ssl.SSLException -> "TLS连接失败"
        else -> failure.message?.takeIf { it.matches(Regex("HTTP_[1-5][0-9]{2}")) }
            ?: failure.javaClass.simpleName
    }

    private fun recordReply(status: String, sbnKey: String?, actionIndex: Int) {
        ProbeRuntime.lastReplyStatus = replyStatusLabel(status)
        val salt = Privacy.salt(this)
        ProbeStore(this).append(
            JSONObject()
                .put("schemaVersion", 1)
                .put("capturedAt", System.currentTimeMillis())
                .put("eventType", "remoteInputTestResult")
                .put("status", status)
                .put("sbnKey", Privacy.redact(sbnKey, salt).hash)
                .put("actionIndex", actionIndex)
                .toString(),
        )
    }

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

    companion object {
        // ponytail: 750ms same-key identical-message ceiling; crash after Room insert before identity persistence can resend, so upgrade to an atomic queue+identity transaction for retries or longer windows.
        private const val SyncDuplicateWindowMillis = 750L
        private const val ActionScanCandidates = "com.aurora.wechatrelay.probe.SCAN_REMOTE_INPUT"
        private const val ActionStartReplyPolling = "com.aurora.wechatrelay.probe.START_REPLY_POLLING"
        private const val ActionReply = "com.aurora.wechatrelay.probe.ONE_TIME_REPLY"
        private const val ActionRetryVoice = "com.aurora.wechatrelay.probe.RETRY_LATEST_VOICE"
        private const val ReplyPollFailureBackoffMillis = 5_000L

        fun requestCandidateScan(context: Context) {
            context.startService(Intent(context, WechatNotificationListenerService::class.java).setAction(ActionScanCandidates))
        }

        fun requestReplyPolling(context: Context) {
            context.startService(Intent(context, WechatNotificationListenerService::class.java).setAction(ActionStartReplyPolling))
        }
        fun requestLatestVoiceRetry(context: Context) {
            context.startService(Intent(context, WechatNotificationListenerService::class.java).setAction(ActionRetryVoice))
        }

        fun requestOneTimeReply(context: Context, candidate: RemoteInputCandidate, replyText: String) {
            if (ProbeRuntime.submitOneTimeReply(candidate, replyText)) {
                context.startService(Intent(context, WechatNotificationListenerService::class.java).setAction(ActionReply))
            }
        }
    }
}
