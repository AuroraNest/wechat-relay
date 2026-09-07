package com.aurora.wechatrelay.probe

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import androidx.work.Constraints
import androidx.work.ExistingWorkPolicy
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.Worker
import androidx.work.WorkerParameters
import org.json.JSONObject
import java.net.URL
import java.nio.charset.StandardCharsets
import java.security.KeyPairGenerator
import java.security.KeyStore
import java.security.SecureRandom
import java.security.Signature
import java.security.spec.ECGenParameterSpec
import java.util.concurrent.atomic.AtomicBoolean
import javax.net.ssl.HttpsURLConnection

data class PairingCode(val pairId: String, val pairSecret: String, val expiresAt: Long, val a2i: ByteArray, val i2a: ByteArray)
data class ReplyCommand(
    val v: Int,
    val id: String,
    val targetMessageId: String,
    val deviceId: String,
    val createdAt: Long,
    val wechatUserId: Int,
    val pairId: String?,
    val replyEnvelope: Envelope,
)
data class ReplyPollResult(val relayPolicy: RelayPolicy, val reply: ReplyCommand?)

internal class PairingGate {
    private val inFlight = AtomicBoolean(false)
    fun tryAcquire(): Boolean = inFlight.compareAndSet(false, true)
    fun release() = inFlight.set(false)
}

object SyncNetwork {
    const val BaseUrl = BuildConfig.RELAY_ORIGIN

    enum class UploadResult { Complete, Retry }

    fun parsePairingCode(value: String): PairingCode {
        require(value.startsWith("AWR1:"))
        val json = org.json.JSONObject(String(SyncProtocol.decode(value.removePrefix("AWR1:")), StandardCharsets.UTF_8))
        require(json.getInt("v") == 1 && json.getString("origin") == BaseUrl && json.getLong("expiresAt") > System.currentTimeMillis())
        val a2i = SyncProtocol.decode(json.getString("kA2I")); val i2a = SyncProtocol.decode(json.getString("kI2A"))
        require(a2i.size == 32 && i2a.size == 32 && json.getString("pairSecret").length >= 43)
        return PairingCode(json.getString("pairId"), json.getString("pairSecret"), json.getLong("expiresAt"), a2i, i2a)
    }

    fun pair(context: Context, code: PairingCode): Boolean {
        var acquired = false
        try {
            acquired = pairingGate.tryAcquire()
            if (!acquired) return false
            val store = SyncStore.get(context)
            store.verifyKeyWrapping()
            val previousAlias = store.signingAliasOrNull()
            val deviceId = ByteArray(16).also(SecureRandom()::nextBytes).let(SyncProtocol::encode)
            val alias = "awrelay_phase1_sign_$deviceId"
            var saved = false
            var connection: HttpsURLConnection? = null
            try {
                val generator = KeyPairGenerator.getInstance(KeyProperties.KEY_ALGORITHM_EC, "AndroidKeyStore")
                generator.initialize(KeyGenParameterSpec.Builder(alias, KeyProperties.PURPOSE_SIGN or KeyProperties.PURPOSE_VERIFY).setAlgorithmParameterSpec(ECGenParameterSpec("secp256r1")).setDigests(KeyProperties.DIGEST_SHA256).build())
                val publicKey = generator.generateKeyPair().public.encoded
                val body = "{\"pairId\":${quote(code.pairId)},\"pairSecret\":${quote(code.pairSecret)},\"deviceId\":${quote(deviceId)},\"publicKey\":${quote(SyncProtocol.encode(publicKey))}}"
                connection = connection("/api/v1/android/pair")
                writeJson(connection, body.toByteArray(StandardCharsets.UTF_8))
                if (connection.responseCode !in 200..299) return false
                store.savePairing(deviceId, alias, code.a2i, code.i2a)
                saved = true
                if (shouldDeleteSigningAlias(previousAlias, alias)) deleteSigningAlias(previousAlias)
                return true
            } finally {
                connection?.disconnect()
                if (!saved) deleteSigningAlias(alias)
            }
        } finally {
            if (acquired) pairingGate.release()
            clearPairingKeys(code)
        }
    }

    fun enqueue(context: Context) {
        val request = OneTimeWorkRequestBuilder<SyncUploadWorker>().setConstraints(Constraints.Builder().setRequiredNetworkType(androidx.work.NetworkType.CONNECTED).build()).build()
        WorkManager.getInstance(context).enqueueUniqueWork("phase1-upload", ExistingWorkPolicy.APPEND_OR_REPLACE, request)
    }

    @Synchronized
    fun uploadPending(context: Context, expectedDeviceId: String? = null): UploadResult {
        val store = SyncStore.get(context)
        if (!store.paired()) return UploadResult.Complete
        if (expectedDeviceId != null && !store.isCurrentDevice(expectedDeviceId)) return UploadResult.Complete
        val signing = store.requestSigningContext(expectedDeviceId)
        for (item in store.pending(signing.deviceId)) {
            if (!store.isCurrentDevice(signing.deviceId)) return UploadResult.Complete
            try {
                store.state(signing.deviceId, item.id, SyncStore.Uploading)
                val assetsJson = store.pendingAssets(signing.deviceId, item.id)
                val body = "{\"v\":5,\"id\":${quote(item.id)},\"deviceId\":${quote(signing.deviceId)},\"wechatUserId\":${item.wechatUserId},\"replyCapable\":${item.replyCapable},\"conversationSendCapable\":${item.conversationSendCapable},\"seq\":${item.seq},\"createdAt\":${item.createdAt},\"previewEnvelope\":${item.envelope},\"assets\":${assetsJson}}".toByteArray(StandardCharsets.UTF_8)
                val connection = signedConnection(signing, "POST", "/api/v1/android/messages", body)
                try {
                    writeJson(connection, body)
                    val responseCode = connection.responseCode
                    val result = uploadResultForHttpStatus(responseCode)
                    val dropped = result == UploadResult.Complete && connection.inputStream.bufferedReader(StandardCharsets.UTF_8).use { it.readText() }.let(::JSONObject).optBoolean("dropped")
                    if (!store.isCurrentDevice(signing.deviceId)) return UploadResult.Complete
                    when (result) {
                        UploadResult.Complete -> {
                            store.state(signing.deviceId, item.id, SyncStore.ServerAccepted)
                            if (dropped) ProbeStore(context).append(JSONObject()
                                .put("eventType", "syncDiagnostic")
                                .put("capturedAt", System.currentTimeMillis())
                                .put("stage", "RELAY_MESSAGE_DROPPED")
                                .toString())
                            else if (assetsJson != "[]") runCatching {
                                val assets = org.json.JSONArray(assetsJson)
                                for (index in 0 until assets.length()) {
                                    val asset = assets.getJSONObject(index)
                                    if (asset.optString("kind") == "image") ProbeStore(context).append(JSONObject()
                                        .put("eventType", "syncDiagnostic")
                                        .put("capturedAt", System.currentTimeMillis())
                                        .put("stage", "IMAGE_SERVER_ACCEPTED")
                                        .put("width", asset.optInt("width"))
                                        .put("height", asset.optInt("height"))
                                        .toString())
                                }
                            }
                        }
                        UploadResult.Retry -> {
                            store.state(signing.deviceId, item.id, SyncStore.Queued)
                            return UploadResult.Retry
                        }
                    }
                } finally {
                    connection.disconnect()
                }
            } catch (_: Exception) {
                try {
                    store.state(signing.deviceId, item.id, SyncStore.Queued)
                } catch (_: Exception) {
                }
                return if (store.isCurrentDevice(signing.deviceId)) UploadResult.Retry else UploadResult.Complete
            }
        }
        return UploadResult.Complete
    }

    /** Performs one server-side long poll. The service owns retry pacing and listener lifetime. */
    fun pollReply(context: Context, expectedDeviceId: String): ReplyPollResult {
        val store = SyncStore.get(context)
        check(store.paired())
        val connection = signedConnection(store.requestSigningContext(expectedDeviceId), "GET", RepliesPath, ByteArray(0), ReplyPollReadTimeoutMillis)
        try {
            val responseCode = connection.responseCode
            if (responseCode !in 200..299) throw IllegalStateException("HTTP_$responseCode")
            val root = connection.inputStream.bufferedReader(StandardCharsets.UTF_8).use { it.readText() }.let(::JSONObject)
            val relayPolicy = RelayPolicy.fromJson(root.getJSONObject("relayPolicy"))
            if (root.isNull("reply")) return ReplyPollResult(relayPolicy, null)
            val reply = root.getJSONObject("reply")
            val version = reply.getInt("v")
            require(version == 1 || version == 2 || version == 3)
            val envelope = reply.getJSONObject("replyEnvelope")
            return ReplyPollResult(relayPolicy, ReplyCommand(
                v = version,
                id = reply.getString("id"),
                targetMessageId = reply.getString("targetMessageId"),
                deviceId = reply.getString("deviceId"),
                createdAt = reply.getLong("createdAt"),
                wechatUserId = reply.getInt("wechatUserId"),
                pairId = reply.optString("pairId").takeIf(String::isNotBlank),
                replyEnvelope = Envelope(
                    alg = envelope.getString("alg"),
                    kid = envelope.getString("kid"),
                    iv = envelope.getString("iv"),
                    aad = envelope.getString("aad"),
                    ct = envelope.getString("ct"),
                ),
            ).also {
                require(it.id.isNotBlank() && it.targetMessageId.isNotBlank() && it.deviceId.isNotBlank() && it.createdAt > 0L && NotificationSnapshot.isAllowedWechatUserId(it.wechatUserId))
            })
        } finally {
            connection.disconnect()
        }
    }

    fun acknowledgeReply(context: Context, expectedDeviceId: String, replyId: String, status: String) {
        require(status in ReplyStatuses)
        require(replyId.isNotBlank() && !replyId.contains('/'))
        val store = SyncStore.get(context)
        val path = "$RepliesPath/$replyId/ack"
        val body = "{\"status\":${quote(status)}}".toByteArray(StandardCharsets.UTF_8)
        val connection = signedConnection(store.requestSigningContext(expectedDeviceId), "POST", path, body)
        try {
            writeJson(connection, body)
            val responseCode = connection.responseCode
            if (responseCode !in 200..299) throw IllegalStateException("HTTP_$responseCode")
        } finally {
            connection.disconnect()
        }
    }

    // Server 4xx errors also represent device-wide or ordering failures, not a safe item-specific terminal contract.
    internal fun uploadResultForHttpStatus(responseCode: Int): UploadResult =
        if (responseCode in 200..299) UploadResult.Complete else UploadResult.Retry

    private fun signedConnection(signing: RequestSigningContext, method: String, path: String, body: ByteArray, readTimeout: Int = 15_000): HttpsURLConnection {
        val timestamp = System.currentTimeMillis().toString()
        val nonce = ByteArray(16).also(SecureRandom()::nextBytes).let(SyncProtocol::encode)
        val canonical = SyncProtocol.canonicalRequest(method, path, timestamp, nonce, body)
        val privateKey = (KeyStore.getInstance("AndroidKeyStore").apply { load(null) }.getKey(signing.signingAlias, null) as java.security.PrivateKey)
        val signature = Signature.getInstance("SHA256withECDSA").apply {
            initSign(privateKey)
            update(canonical.toByteArray(StandardCharsets.UTF_8))
        }.sign()
        return connection(path, method, readTimeout).apply {
            setRequestProperty("X-AWR-Device-Id", signing.deviceId)
            setRequestProperty("X-AWR-Timestamp", timestamp)
            setRequestProperty("X-AWR-Nonce", nonce)
            setRequestProperty("X-AWR-Signature", SyncProtocol.encode(signature))
        }
    }

    internal fun connection(path: String, method: String = "POST", readTimeout: Int = 15_000): HttpsURLConnection =
        (URL(BaseUrl + path).openConnection() as HttpsURLConnection).apply {
            requestMethod = method
            connectTimeout = 15_000
            this.readTimeout = readTimeout
            doOutput = method == "POST"
            if (method == "POST") setRequestProperty("Content-Type", "application/json")
        }
    internal fun writeJson(connection: HttpsURLConnection, bytes: ByteArray) { connection.setFixedLengthStreamingMode(bytes.size); connection.outputStream.use { it.write(bytes) } }
    internal fun quote(value: String): String = org.json.JSONObject.quote(value)

    internal fun shouldDeleteSigningAlias(candidate: String?, retained: String): Boolean = candidate != null && candidate != retained
    internal fun clearPairingKeys(code: PairingCode) {
        code.a2i.fill(0)
        code.i2a.fill(0)
    }
    private fun deleteSigningAlias(alias: String?) {
        if (alias == null) return
        runCatching {
            val keyStore = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
            if (keyStore.containsAlias(alias)) keyStore.deleteEntry(alias)
        }
    }

    private const val RepliesPath = "/api/v1/android/replies"
    private const val ReplyPollReadTimeoutMillis = 30_000
    private val pairingGate = PairingGate()
    private val ReplyStatuses = setOf(
        "SENT_TO_WECHAT", "NOTIFICATION_NOT_ACTIVE", "WECHAT_ACTION_CHANGED", "REMOTE_INPUT_UNSUPPORTED",
        "PENDING_INTENT_CANCELED", "INVALID_REPLY", "FAILED",
    )
}

class SyncUploadWorker(context: Context, parameters: WorkerParameters) : Worker(context, parameters) {
    override fun doWork(): Result = when (SyncNetwork.uploadPending(applicationContext)) {
        SyncNetwork.UploadResult.Complete -> Result.success()
        SyncNetwork.UploadResult.Retry -> Result.retry()
    }
}
