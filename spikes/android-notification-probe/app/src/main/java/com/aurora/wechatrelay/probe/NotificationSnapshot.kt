package com.aurora.wechatrelay.probe

import android.app.Notification
import android.os.Bundle
import android.os.Parcelable
import android.service.notification.StatusBarNotification
import org.json.JSONArray
import org.json.JSONObject

data class CapturedNotification(
    val json: JSONObject,
    val fingerprint: String,
    val remoteInputCandidates: List<RemoteInputCandidate>,
)

object NotificationSnapshot {
    const val WechatPackage = "com.tencent.mm"
    const val WechatMessageChannelId = "message_channel_new_id"

    fun isAllowedWechatUserId(userId: Int): Boolean = userId == 0 || userId == 999

    fun isAllowedWechatSource(packageName: String, userId: Int, channelId: String?): Boolean =
        packageName == WechatPackage && isAllowedWechatUserId(userId) && channelId == WechatMessageChannelId

    @Suppress("DEPRECATION")
    fun capture(context: android.content.Context, sbn: StatusBarNotification): CapturedNotification {
        val salt = Privacy.salt(context)
        val notification = sbn.notification
        val extras = notification.extras ?: Bundle.EMPTY
        val actions = notification.actions ?: emptyArray()
        val keyRedaction = Privacy.redact(sbn.key, salt)
        val actionJson = JSONArray()
        val candidates = mutableListOf<RemoteInputCandidate>()

        actions.forEachIndexed { index, action ->
            val remoteInputs = action.remoteInputs ?: emptyArray()
            val creatorPackage = action.actionIntent?.creatorPackage
            val remoteInputJson = JSONArray()
            remoteInputs.forEach { remoteInput ->
                remoteInputJson.put(
                    JSONObject()
                        .put("resultKey", redactedJson(Privacy.redact(remoteInput.resultKey, salt)))
                        .put("allowFreeFormInput", remoteInput.allowFreeFormInput),
                )
            }
            actionJson.put(
                JSONObject()
                    .put("title", redactedJson(Privacy.redact(action.title, salt)))
                    .put("semanticAction", action.semanticAction)
                    .put("remoteInputs", remoteInputJson)
                    .put("pendingIntentCreatorPackage", creatorPackage),
            )
            if (isReplyCapable(creatorPackage, remoteInputs.map { it.allowFreeFormInput })) {
                candidates += RemoteInputCandidate(
                    sbnKey = sbn.key,
                    wechatUserId = sbn.userId,
                    displayKeyHash = keyRedaction.hash.orEmpty(),
                    actionIndex = index,
                    remoteInputCount = remoteInputs.size,
                )
            }
        }

        val event = JSONObject()
            .put("schemaVersion", 1)
            .put("capturedAt", System.currentTimeMillis())
            .put("packageName", sbn.packageName)
            .put("userHandle", sbn.userId)
            .put("sbnKey", redactedJson(keyRedaction))
            .put("id", sbn.id)
            .put("tag", redactedJson(Privacy.redact(sbn.tag, salt)))
            .put("postTime", sbn.postTime)
            .put("channelId", notification.channelId)
            .put("category", notification.category)
            .put("groupKey", redactedJson(Privacy.redact(sbn.groupKey, salt)))
            .put("isGroup", sbn.isGroup)
            .put("isGroupSummary", (notification.flags and Notification.FLAG_GROUP_SUMMARY) != 0)
            .put("when", notification.`when`)
            .put("extras", describeBundle(extras, salt))
            .put("messagingStyle", describeMessagingStyle(extras, salt))
            .put("actions", actionJson)

        val fingerprintInput = JSONObject(event.toString()).apply { remove("capturedAt") }
        val fingerprint = Privacy.saltedHash(fingerprintInput.toString(), salt)
        event.put("sourceEventHash", fingerprint)
        return CapturedNotification(event, fingerprint, candidates)
    }

    internal fun isReplyCapable(creatorPackage: String?, allowsFreeFormInput: List<Boolean>): Boolean =
        creatorPackage == WechatPackage && allowsFreeFormInput.any { it }

    fun hasWechatContentIntent(sbn: StatusBarNotification): Boolean =
        sbn.notification.contentIntent?.creatorPackage == WechatPackage

    fun isConversationSendCandidate(sbn: StatusBarNotification): Boolean =
        isConversationSendCandidate(
            sbn.notification.category,
            (sbn.notification.flags and Notification.FLAG_GROUP_SUMMARY) != 0,
        )

    internal fun isConversationSendCandidate(category: String?, isGroupSummary: Boolean): Boolean =
        category == Notification.CATEGORY_MESSAGE && !isGroupSummary

    fun conversationTitleHash(context: android.content.Context, sbn: StatusBarNotification): String {
        val extras = sbn.notification.extras ?: Bundle.EMPTY
        val title = LockscreenReplySelectors.normalizeTitle(
            extras.getCharSequence(Notification.EXTRA_CONVERSATION_TITLE)
                ?: extras.getCharSequence(Notification.EXTRA_TITLE),
        )
        return title.takeIf(String::isNotEmpty)?.let { Privacy.saltedHash(it, Privacy.salt(context)) }.orEmpty()
    }

    internal fun targetMatches(
        expectedSbnKey: String,
        expectedWechatUserId: Int,
        expectedTitleHash: String,
        actualSbnKey: String,
        actualWechatUserId: Int,
        actualTitleHash: String,
    ): Boolean = expectedSbnKey == actualSbnKey &&
        expectedWechatUserId == actualWechatUserId &&
        expectedTitleHash.isNotBlank() &&
        expectedTitleHash == actualTitleHash

    @Suppress("DEPRECATION")
    fun preview(sbn: StatusBarNotification): Preview? {
        val extras = sbn.notification.extras ?: Bundle.EMPTY
        val messages = extras.getParcelableArray(Notification.EXTRA_MESSAGES)
        val latest = messages?.lastOrNull() as? Bundle
        if (latest != null) {
            val text = latest.getCharSequence("text")
            val sender = latest.getCharSequence("sender") ?: extras.getCharSequence(Notification.EXTRA_TITLE)
            if (!text.isNullOrBlank()) return SyncProtocol.normalizePreview(sender, text, "messaging")
        }
        val bigText = extras.getCharSequence(Notification.EXTRA_BIG_TEXT)
        if (!bigText.isNullOrBlank()) return SyncProtocol.normalizePreview(extras.getCharSequence(Notification.EXTRA_TITLE), bigText, "big_text")
        val text = extras.getCharSequence(Notification.EXTRA_TEXT)
        if (!text.isNullOrBlank()) return SyncProtocol.normalizePreview(extras.getCharSequence(Notification.EXTRA_TITLE), text, "text")
        val lines = extras.getCharSequenceArray(Notification.EXTRA_TEXT_LINES)?.joinToString("\n")
        if (!lines.isNullOrBlank()) return SyncProtocol.normalizePreview(extras.getCharSequence(Notification.EXTRA_TITLE), lines, "text_lines")
        val subText = extras.getCharSequence(Notification.EXTRA_SUB_TEXT)
        if (!subText.isNullOrBlank()) return SyncProtocol.normalizePreview(extras.getCharSequence(Notification.EXTRA_TITLE), subText, "sub_text")
        return null
    }

    @Suppress("DEPRECATION")
    private fun describeMessagingStyle(extras: Bundle, salt: ByteArray): JSONObject {
        val messages = extras.getParcelableArray(Notification.EXTRA_MESSAGES)
        val historicMessages = extras.getParcelableArray(Notification.EXTRA_HISTORIC_MESSAGES)
        return JSONObject()
            .put("present", messages != null || extras.containsKey(Notification.EXTRA_MESSAGING_PERSON))
            .put("messages", describeMessageBundles(messages, salt))
            .put("historicMessages", describeMessageBundles(historicMessages, salt))
    }

    private fun describeMessageBundles(values: Array<Parcelable>?, salt: ByteArray): JSONArray {
        val result = JSONArray()
        values?.forEach { value ->
            val bundle = value as? Bundle
            result.put(
                if (bundle == null) {
                    JSONObject().put("type", value.javaClass.name).put("fields", JSONArray())
                } else {
                    JSONObject().put("type", "android.os.Bundle").put("fields", describeBundle(bundle, salt))
                },
            )
        }
        return result
    }

    private fun describeBundle(bundle: Bundle, salt: ByteArray): JSONArray {
        val fields = JSONArray()
        bundle.keySet().sorted().forEach { key ->
            val value = runCatching { bundle.get(key) }.getOrNull()
            fields.put(JSONObject().put("name", key).put("value", redactedJson(Privacy.redact(value, salt))))
        }
        return fields
    }

    private fun redactedJson(value: RedactedValue): JSONObject = JSONObject()
        .put("type", value.type)
        .put("length", value.length)
        .put("saltedHash", value.hash)
}
