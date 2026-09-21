package com.aurora.wechatrelay.probe

import android.content.Context
import android.content.pm.LauncherApps
import android.os.UserManager
import android.util.AtomicFile
import androidx.work.Constraints
import androidx.work.ExistingWorkPolicy
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.Worker
import androidx.work.WorkerParameters
import java.io.File
import java.nio.charset.StandardCharsets
import org.json.JSONObject

data class ContactsProfile(val wechatUserId: Int, val userSerial: Long, val label: String)
data class ContactsPending(
    val id: String,
    val deviceId: String,
    val wechatUserId: Int,
    val userSerial: Long?,
    val capturedAt: Long,
    val envelope: Envelope,
    val v: Int,
)

data class ContactsCurrent(
    val id: String,
    val deviceId: String,
    val wechatUserId: Int,
    val userSerial: Long,
    val capturedAt: Long,
    val envelope: Envelope,
)

private object ContactsStorageLock
private val ContactsUuidV7 = Regex("[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")

internal object ContactsSendPolicy {
    fun matches(current: ContactsCurrent, snapshotId: String, deviceId: String, wechatUserId: Int): Boolean =
        current.id == snapshotId && current.deviceId == deviceId && current.wechatUserId == wechatUserId

    fun hasExactMember(contacts: List<String>, title: String): Boolean = contacts.any { it == title }

    fun terminalStatus(status: String, stage: String): String = when (stage) {
        "ACCESSIBILITY_NOT_ARMED" -> "AUTOMATION_NOT_READY"
        "WECHAT_WINDOW_TIMEOUT" -> "WECHAT_WINDOW_TIMEOUT"
        else -> status
    }
}

internal object ContactsScanPolicy {
    fun hasCompleteProof(startedAtTop: Boolean, expectedCount: Int?, uniqueNameCount: Int): Boolean =
        startedAtTop && expectedCount != null && expectedCount == uniqueNameCount
    fun stableFor(signature: Int?, sinceMillis: Long, currentSignature: Int, nowMillis: Long, minimumMillis: Long): Boolean =
        signature == currentSignature && nowMillis - sinceMillis >= minimumMillis
}

/** Pure limits and page assembly, kept independent from Accessibility node lifecycle. */
class ContactsPageAssembly(private val maximumContacts: Int = MaxContacts) {
    private val names = LinkedHashSet<String>()
    var startedAtTop: Boolean = false
        private set
    var expectedCount: Int? = null
        private set

    fun confirmTop() { startedAtTop = true }

    fun addStablePage(values: List<CharSequence>) {
        values.forEach { raw ->
            val name = raw.toString().trim()
            if (name in SystemEntries) return@forEach
            require(name.isNotEmpty() && name.toByteArray(StandardCharsets.UTF_8).size <= MaxNameBytes)
            names.add(name)
            require(names.size <= maximumContacts)
        }
        require(plaintextJson().toByteArray(StandardCharsets.UTF_8).size <= MaxPlaintextBytes)
    }

    fun observeFooter(count: Int) {
        require(count in 0..maximumContacts)
        expectedCount = count
    }

    fun complete(): List<String> {
        check(ContactsScanPolicy.hasCompleteProof(startedAtTop, expectedCount, names.size))
        return names.toList()
    }

    fun count(): Int = names.size
    fun plaintextJson(): String = contactsJson(names.toList())

    companion object {
        const val MaxContacts = 10_000
        const val MaxNameBytes = 512
        const val MaxPlaintextBytes = 1_048_576
        // ponytail: Chinese WeChat 8.0.77 excludes these from its friend total; revisit on locale or UI changes.
        private val SystemEntries = setOf("微信团队", "文件传输助手")

        fun contactsJson(names: List<String>): String {
            return buildString {
                append("{\"v\":1,\"contacts\":[")
                names.forEachIndexed { index, name ->
                    if (index > 0) append(',')
                    append("{\"name\":")
                    appendJsonString(name)
                    append('}')
                }
                append("]}")
            }
        }

        private fun StringBuilder.appendJsonString(value: String) {
            append('"')
            value.forEach { character ->
                when (character) {
                    '"' -> append("\\\"")
                    '\\' -> append("\\\\")
                    '\b' -> append("\\b")
                    '\u000c' -> append("\\f")
                    '\n' -> append("\\n")
                    '\r' -> append("\\r")
                    '\t' -> append("\\t")
                    else -> if (character.code < 0x20) append("\\u${character.code.toString(16).padStart(4, '0')}") else append(character)
                }
            }
            append('"')
        }
    }
}

object ContactsProfileResolver {
    fun isAvailable(context: Context, profile: ContactsProfile): Boolean {
        if (profile.wechatUserId == 0) {
            return context.packageManager.getLaunchIntentForPackage(NotificationSnapshot.WechatPackage)
                ?.component?.packageName == NotificationSnapshot.WechatPackage
        }
        if (profile.wechatUserId != 999 || profile.userSerial < 0L) return false
        val launcher = context.getSystemService(LauncherApps::class.java)
        val user = context.getSystemService(UserManager::class.java).getUserForSerialNumber(profile.userSerial)
            ?.takeIf { candidate -> launcher.profiles.count { it == candidate } == 1 } ?: return false
        return launcher.getActivityList(NotificationSnapshot.WechatPackage, user)
            .count { it.componentName.packageName == NotificationSnapshot.WechatPackage } == 1
    }

    fun installed(context: Context): List<ContactsProfile> {
        val result = ArrayList<ContactsProfile>()
        val packageManager = context.packageManager
        if (packageManager.getLaunchIntentForPackage(NotificationSnapshot.WechatPackage)
                ?.component?.packageName == NotificationSnapshot.WechatPackage
        ) result += ContactsProfile(0, 0L, "主微信")

        val launcher = context.getSystemService(LauncherApps::class.java)
        val users = context.getSystemService(UserManager::class.java)
        // Android's public API does not expose a UserHandle numeric id. Reuse only the serial
        // observed on a WeChat notification already bound to profile 999; never guess a clone.
        val cloneSerial = runCatching { SyncStore.get(context).knownWechatUserSerial(999) }.getOrNull()
        val clone = cloneSerial?.let(users::getUserForSerialNumber)
            ?.takeIf { candidate -> launcher.profiles.count { profile -> profile == candidate } == 1 }
        if (clone != null) {
            val activity = launcher.getActivityList(NotificationSnapshot.WechatPackage, clone)
                .singleOrNull { it.componentName.packageName == NotificationSnapshot.WechatPackage }
            if (activity != null) result += ContactsProfile(999, requireNotNull(cloneSerial), "分身微信")
        }
        return result
    }

}

/** Stores only an already encrypted snapshot. Names never reach disk outside its AES-GCM envelope. */
class ContactsPendingStore private constructor(context: Context) {
    private val appContext = context.applicationContext

    fun save(value: ContactsPending) = synchronized(ContactsStorageLock) {
        require(value.id == value.id.lowercase() && ContactsUuidV7.matches(value.id))
        require(NotificationSnapshot.isAllowedWechatUserId(value.wechatUserId))
        require(value.v in 1..2 && (value.v == 1 || value.userSerial != null))
        require(value.userSerial == null || value.wechatUserId != 0 || value.userSerial == 0L)
        val target = AtomicFile(fileFor(value.wechatUserId))
        val bytes = JSONObject()
            .put("v", value.v)
            .put("id", value.id)
            .put("deviceId", value.deviceId)
            .put("wechatUserId", value.wechatUserId)
            .putOpt("userSerial", value.userSerial)
            .put("capturedAt", value.capturedAt)
            .put("contactsEnvelope", JSONObject(SyncProtocol.envelopeJson(value.envelope)))
            .toString().toByteArray(StandardCharsets.UTF_8)
        val stream = target.startWrite()
        try {
            stream.write(bytes)
            target.finishWrite(stream)
        } catch (failure: Exception) {
            target.failWrite(stream)
            throw failure
        }
    }

    fun read(wechatUserId: Int): ContactsPending? = synchronized(ContactsStorageLock) {
        val file = fileFor(wechatUserId)
        if (!file.exists()) return null
        val root = JSONObject(AtomicFile(file).openRead().bufferedReader(StandardCharsets.UTF_8).use { it.readText() })
        val envelope = root.getJSONObject("contactsEnvelope")
        return ContactsPending(
            id = root.getString("id"),
            deviceId = root.getString("deviceId"),
            wechatUserId = root.getInt("wechatUserId"),
            userSerial = if (root.has("userSerial")) root.getLong("userSerial") else null,
            capturedAt = root.getLong("capturedAt"),
            envelope = Envelope(envelope.getString("alg"), envelope.getString("kid"), envelope.getString("iv"), envelope.getString("aad"), envelope.getString("ct")),
            v = root.optInt("v", 1),
        ).also { validate(it, wechatUserId) }
    }

    fun clear(wechatUserId: Int) = synchronized(ContactsStorageLock) { AtomicFile(fileFor(wechatUserId)).delete() }
    fun clearAll() = synchronized(ContactsStorageLock) { AtomicFile(fileFor(0)).delete(); AtomicFile(fileFor(999)).delete() }

    /** The upload that observed an older file must never erase a newer completed scan. */
    fun clearIfMatches(wechatUserId: Int, id: String, deviceId: String): Boolean = synchronized(ContactsStorageLock) {
        val current = read(wechatUserId) ?: return@synchronized false
        if (current.id != id || current.deviceId != deviceId) return@synchronized false
        AtomicFile(fileFor(wechatUserId)).delete()
        true
    }

    fun isCurrent(wechatUserId: Int, id: String, deviceId: String): Boolean = synchronized(ContactsStorageLock) {
        val current = read(wechatUserId)
        current?.id == id && current.deviceId == deviceId
    }

    private fun validate(value: ContactsPending, requestedUserId: Int) {
        require(value.wechatUserId == requestedUserId && NotificationSnapshot.isAllowedWechatUserId(value.wechatUserId))
        require(value.v in 1..2 && value.id == value.id.lowercase() && ContactsUuidV7.matches(value.id) && value.capturedAt > 0L)
        require(value.v == 1 || value.userSerial != null)
        require(value.userSerial == null || value.wechatUserId != 0 || value.userSerial == 0L)
        require(value.envelope.alg == "A256GCM" && value.envelope.kid == "phase1-contacts")
        require(value.envelope.aad == if (value.v == 1) SyncProtocol.a2iContactsAad(value.id, value.deviceId, value.capturedAt, value.wechatUserId) else SyncProtocol.a2iContactsV2Aad(value.id, value.deviceId, value.capturedAt, value.wechatUserId))
        require(SyncProtocol.decode(value.envelope.iv).size == 12)
        require(SyncProtocol.decode(value.envelope.ct).size <= MaxCiphertextBytes)
    }

    private fun fileFor(wechatUserId: Int): File = File(appContext.filesDir, "contacts-pending-$wechatUserId.json")

    companion object {
        const val MaxCiphertextBytes = ContactsPageAssembly.MaxPlaintextBytes + 16
        fun get(context: Context): ContactsPendingStore = ContactsPendingStore(context)
    }
}

/** Keeps the latest server-accepted v2 encrypted scan. No contact plaintext is persisted here. */
class ContactsCurrentStore private constructor(context: Context) {
    private val appContext = context.applicationContext

    fun save(value: ContactsCurrent) = synchronized(ContactsStorageLock) {
        validate(value, value.wechatUserId)
        val target = AtomicFile(fileFor(value.wechatUserId))
        val bytes = JSONObject()
            .put("v", 2)
            .put("id", value.id)
            .put("deviceId", value.deviceId)
            .put("wechatUserId", value.wechatUserId)
            .put("userSerial", value.userSerial)
            .put("capturedAt", value.capturedAt)
            .put("contactsEnvelope", JSONObject(SyncProtocol.envelopeJson(value.envelope)))
            .toString().toByteArray(StandardCharsets.UTF_8)
        val stream = target.startWrite()
        try {
            stream.write(bytes)
            target.finishWrite(stream)
        } catch (failure: Exception) {
            target.failWrite(stream)
            throw failure
        }
    }

    fun read(wechatUserId: Int): ContactsCurrent? = synchronized(ContactsStorageLock) {
        val file = fileFor(wechatUserId)
        if (!file.exists()) return null
        val root = JSONObject(AtomicFile(file).openRead().bufferedReader(StandardCharsets.UTF_8).use { it.readText() })
        require(root.getInt("v") == 2)
        val envelope = root.getJSONObject("contactsEnvelope")
        return ContactsCurrent(
            id = root.getString("id"),
            deviceId = root.getString("deviceId"),
            wechatUserId = root.getInt("wechatUserId"),
            userSerial = root.getLong("userSerial"),
            capturedAt = root.getLong("capturedAt"),
            envelope = Envelope(envelope.getString("alg"), envelope.getString("kid"), envelope.getString("iv"), envelope.getString("aad"), envelope.getString("ct")),
        ).also { validate(it, wechatUserId) }
    }

    fun promoteIfPendingCurrent(pendingStore: ContactsPendingStore, pending: ContactsPending): Boolean = synchronized(ContactsStorageLock) {
        if (pending.v != 2 || pending.userSerial == null || !pendingStore.isCurrent(pending.wechatUserId, pending.id, pending.deviceId)) return false
        save(ContactsCurrent(pending.id, pending.deviceId, pending.wechatUserId, pending.userSerial, pending.capturedAt, pending.envelope))
        true
    }

    fun clearAll() = synchronized(ContactsStorageLock) { AtomicFile(fileFor(0)).delete(); AtomicFile(fileFor(999)).delete() }

    private fun validate(value: ContactsCurrent, requestedUserId: Int) {
        require(value.wechatUserId == requestedUserId && NotificationSnapshot.isAllowedWechatUserId(value.wechatUserId))
        require(value.id == value.id.lowercase() && ContactsUuidV7.matches(value.id) && value.capturedAt > 0L && value.userSerial >= 0L)
        require(value.wechatUserId != 0 || value.userSerial == 0L)
        require(value.envelope.alg == "A256GCM" && value.envelope.kid == "phase1-contacts")
        require(value.envelope.aad == SyncProtocol.a2iContactsV2Aad(value.id, value.deviceId, value.capturedAt, value.wechatUserId))
        require(SyncProtocol.decode(value.envelope.iv).size == 12)
        require(SyncProtocol.decode(value.envelope.ct).size <= ContactsPendingStore.MaxCiphertextBytes)
    }

    private fun fileFor(wechatUserId: Int): File = File(appContext.filesDir, "contacts-current-$wechatUserId.json")

    companion object {
        fun get(context: Context): ContactsCurrentStore = ContactsCurrentStore(context)
    }
}

object ContactsSyncNetwork {
    enum class Result { Complete, Retry, Rejected }

    fun enqueue(context: Context) {
        val request = OneTimeWorkRequestBuilder<ContactsUploadWorker>()
            .setConstraints(Constraints.Builder().setRequiredNetworkType(androidx.work.NetworkType.CONNECTED).build())
            .build()
        WorkManager.getInstance(context).enqueueUniqueWork("phase1-contacts-upload", ExistingWorkPolicy.APPEND_OR_REPLACE, request)
    }

    fun uploadPending(context: Context): Result {
        val store = SyncStore.get(context)
        if (!store.paired()) return Result.Complete
        val signing = store.requestSigningContext()
        val pendingStore = ContactsPendingStore.get(context)
        val currentStore = ContactsCurrentStore.get(context)
        for (userId in listOf(0, 999)) {
            val pending = try { pendingStore.read(userId) } catch (_: Exception) { return Result.Retry } ?: continue
            if (pending.deviceId != signing.deviceId) {
                pendingStore.clearIfMatches(userId, pending.id, pending.deviceId)
                continue
            }
            val body = JSONObject()
                .put("v", pending.v).put("id", pending.id).put("deviceId", pending.deviceId)
                .put("wechatUserId", pending.wechatUserId).put("capturedAt", pending.capturedAt)
                .put("contactsEnvelope", JSONObject(SyncProtocol.envelopeJson(pending.envelope)))
                .toString().toByteArray(StandardCharsets.UTF_8)
            try {
                val connection = SyncNetwork.signedConnection(signing, "POST", "/api/v1/android/contacts", body)
                try {
                    SyncNetwork.writeJson(connection, body)
                    when {
                        connection.responseCode in 200..299 -> {
                            if (!store.isCurrentDevice(pending.deviceId)) return Result.Complete
                            currentStore.promoteIfPendingCurrent(pendingStore, pending)
                            if (pendingStore.clearIfMatches(userId, pending.id, pending.deviceId) && ProbeRuntime.contactsPendingId == pending.id) {
                                ProbeRuntime.contactsPendingId = null
                                ProbeRuntime.contactsScanStatus = "好友已同步"
                            }
                        }
                        connection.responseCode == 409 -> {
                            if (pendingStore.isCurrent(userId, pending.id, pending.deviceId)) {
                                ProbeRuntime.contactsScanStatus = "联系人快照被服务器拒绝, 请重新扫描"
                            }
                            return Result.Rejected
                        }
                        else -> return Result.Retry
                    }
                } finally { connection.disconnect() }
            } catch (_: Exception) {
                return Result.Retry
            }
        }
        return Result.Complete
    }
}

class ContactsUploadWorker(context: Context, parameters: WorkerParameters) : Worker(context, parameters) {
    override fun doWork(): Result = when (ContactsSyncNetwork.uploadPending(applicationContext)) {
        ContactsSyncNetwork.Result.Complete -> Result.success()
        ContactsSyncNetwork.Result.Retry -> Result.retry()
        ContactsSyncNetwork.Result.Rejected -> Result.failure()
    }
}
