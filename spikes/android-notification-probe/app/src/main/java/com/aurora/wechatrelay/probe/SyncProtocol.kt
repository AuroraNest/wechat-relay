package com.aurora.wechatrelay.probe

import java.nio.charset.StandardCharsets
import java.security.MessageDigest
import java.security.SecureRandom
import javax.crypto.Cipher
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.SecretKeySpec

data class Preview(val sender: String, val body: String, val type: String)
data class Envelope(val alg: String, val kid: String, val iv: String, val aad: String, val ct: String)
data class MediaAsset(val id: String, val kind: String, val mimeType: String, val width: Int, val height: Int, val envelope: Envelope)
data class DecryptedReply(val body: String, val conversationTitle: String?)

object SyncProtocol {
    private val random = SecureRandom()

    fun uuidV7(now: Long = System.currentTimeMillis()): String {
        val bytes = ByteArray(16).also(random::nextBytes)
        for (index in 0..5) bytes[index] = (now shr (40 - index * 8)).toByte()
        bytes[6] = ((bytes[6].toInt() and 0x0f) or 0x70).toByte()
        bytes[8] = ((bytes[8].toInt() and 0x3f) or 0x80).toByte()
        val hex = bytes.joinToString("") { "%02x".format(it) }
        return "${hex.substring(0, 8)}-${hex.substring(8, 12)}-${hex.substring(12, 16)}-${hex.substring(16, 20)}-${hex.substring(20)}"
    }

    fun normalizePreview(sender: CharSequence?, body: CharSequence?, type: String): Preview {
        val safeSender = sender?.toString()?.trim().orEmpty().ifBlank { "微信" }
        val characters = body?.toString()?.trim()?.codePoints()?.toArray()?.toList().orEmpty()
        val encoder = StandardCharsets.UTF_8
        for (end in characters.size downTo 0) {
            val safeBody = String(characters.take(end).toIntArray(), 0, end)
            val preview = Preview(safeSender, safeBody, type)
            if (previewJson(preview).toByteArray(encoder).size <= 600) return preview
        }
        return Preview("微信", "", type)
    }

    fun encrypt(key: ByteArray, id: String, deviceId: String, seq: Long, createdAt: Long, preview: Preview, wechatUserId: Int? = null): Envelope {
        require(key.size == 32)
        require(wechatUserId == null || NotificationSnapshot.isAllowedWechatUserId(wechatUserId))
        val iv = ByteArray(12).also(random::nextBytes)
        val aad = a2iAad(id, deviceId, seq, createdAt, wechatUserId)
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, SecretKeySpec(key, "AES"), GCMParameterSpec(128, iv))
        cipher.updateAAD(aad.toByteArray(StandardCharsets.UTF_8))
        val ciphertext = cipher.doFinal(previewJson(preview).toByteArray(StandardCharsets.UTF_8))
        return Envelope("A256GCM", "phase1", encode(iv), aad, encode(ciphertext))
    }

    fun encryptAsset(key: ByteArray, assetId: String, deviceId: String, seq: Long, createdAt: Long, bytes: ByteArray, wechatUserId: Int? = null): Envelope {
        require(key.size == 32 && bytes.size <= 8 * 1024 * 1024)
        require(wechatUserId == null || NotificationSnapshot.isAllowedWechatUserId(wechatUserId))
        val iv = ByteArray(12).also(random::nextBytes)
        val aad = a2iAssetAad(assetId, deviceId, seq, createdAt, wechatUserId)
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, SecretKeySpec(key, "AES"), GCMParameterSpec(128, iv))
        cipher.updateAAD(aad.toByteArray(StandardCharsets.UTF_8))
        return Envelope("A256GCM", "phase1-asset", encode(iv), aad, encode(cipher.doFinal(bytes)))
    }

    fun a2iAad(id: String, deviceId: String, seq: Long, createdAt: Long, wechatUserId: Int? = null): String =
        if (wechatUserId == null) "AWR1|A2I|$id|$deviceId|$seq|$createdAt" else "AWR1|A2I|$id|$deviceId|$seq|$createdAt|$wechatUserId"

    fun a2iAssetAad(assetId: String, deviceId: String, seq: Long, createdAt: Long, wechatUserId: Int? = null): String =
        if (wechatUserId == null) "AWR1|A2I_ASSET|$assetId|$deviceId|$seq|$createdAt" else "AWR1|A2I_ASSET|$assetId|$deviceId|$seq|$createdAt|$wechatUserId"

    fun i2aAad(replyId: String, deviceId: String, targetMessageId: String, createdAt: Long, wechatUserId: Int? = null): String =
        if (wechatUserId == null) "AWR1|I2A|$replyId|$deviceId|$targetMessageId|$createdAt" else "AWR1|I2A|$replyId|$deviceId|$targetMessageId|$createdAt|$wechatUserId"

    fun i2aConversationSendAad(pairId: String, replyId: String, deviceId: String, targetMessageId: String, createdAt: Long, wechatUserId: Int): String {
        require(pairId.isNotBlank() && NotificationSnapshot.isAllowedWechatUserId(wechatUserId))
        return "AWR1|I2A|3|CONVERSATION_SEND|$pairId|$replyId|$deviceId|$targetMessageId|$createdAt|$wechatUserId"
    }

    fun decryptI2aReply(key: ByteArray, replyId: String, deviceId: String, targetMessageId: String, createdAt: Long, envelope: Envelope, wechatUserId: Int? = null): String {
        require(key.size == 32)
        require(wechatUserId == null || NotificationSnapshot.isAllowedWechatUserId(wechatUserId))
        require(envelope.alg == "A256GCM" && envelope.kid == "phase1-reply")
        val expectedAad = i2aAad(replyId, deviceId, targetMessageId, createdAt, wechatUserId)
        require(envelope.aad == expectedAad)
        val iv = decode(envelope.iv)
        require(iv.size == 12)
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.DECRYPT_MODE, SecretKeySpec(key, "AES"), GCMParameterSpec(128, iv))
        cipher.updateAAD(expectedAad.toByteArray(StandardCharsets.UTF_8))
        return parseReplyBody(cipher.doFinal(decode(envelope.ct)).toString(StandardCharsets.UTF_8))
    }

    fun decryptI2aConversationSend(
        key: ByteArray,
        pairId: String,
        replyId: String,
        deviceId: String,
        targetMessageId: String,
        createdAt: Long,
        envelope: Envelope,
        wechatUserId: Int,
    ): DecryptedReply {
        require(key.size == 32 && envelope.alg == "A256GCM" && envelope.kid == "phase1-reply")
        val expectedAad = i2aConversationSendAad(pairId, replyId, deviceId, targetMessageId, createdAt, wechatUserId)
        require(envelope.aad == expectedAad)
        val iv = decode(envelope.iv)
        require(iv.size == 12)
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.DECRYPT_MODE, SecretKeySpec(key, "AES"), GCMParameterSpec(128, iv))
        cipher.updateAAD(expectedAad.toByteArray(StandardCharsets.UTF_8))
        return parseConversationSend(cipher.doFinal(decode(envelope.ct)).toString(StandardCharsets.UTF_8))
    }

    // The downlink schema intentionally has one string field, so keep parsing local and JVM-testable.
    internal fun parseReplyBody(json: String): String {
        require(json.startsWith("{\"body\":\"") && json.endsWith("\"}"))
        val encoded = json.substring(9, json.length - 2)
        val body = decodeJsonString(encoded)
        require(body.isNotBlank() && body.codePointCount(0, body.length) <= 1_000)
        return body
    }

    internal fun parseConversationSend(json: String): DecryptedReply {
        val prefix = "{\"body\":\""
        val separator = "\",\"conversationTitle\":\""
        require(json.startsWith(prefix) && json.endsWith("\"}"))
        val separatorIndex = json.indexOf(separator, prefix.length)
        require(separatorIndex >= prefix.length)
        val body = decodeJsonString(json.substring(prefix.length, separatorIndex))
        val conversationTitle = decodeJsonString(json.substring(separatorIndex + separator.length, json.length - 2))
        require(body.isNotBlank() && body.codePointCount(0, body.length) <= 1_000)
        require(conversationTitle.isNotBlank() && conversationTitle.codePointCount(0, conversationTitle.length) <= 120)
        return DecryptedReply(body, conversationTitle)
    }

    private fun decodeJsonString(value: String): String {
        val decoded = StringBuilder(value.length)
        var index = 0
        while (index < value.length) {
            val character = value[index++]
            if (character != '\\') {
                require(character.code >= 0x20 && character != '"')
                decoded.append(character)
                continue
            }
            require(index < value.length)
            when (val escaped = value[index++]) {
                '"', '\\', '/' -> decoded.append(escaped)
                'b' -> decoded.append('\b')
                'f' -> decoded.append('\u000C')
                'n' -> decoded.append('\n')
                'r' -> decoded.append('\r')
                't' -> decoded.append('\t')
                'u' -> {
                    require(index + 4 <= value.length)
                    val codePoint = value.substring(index, index + 4).toIntOrNull(16) ?: throw IllegalArgumentException("invalid unicode escape")
                    decoded.append(codePoint.toChar())
                    index += 4
                }
                else -> throw IllegalArgumentException("invalid JSON escape")
            }
        }
        return decoded.toString()
    }

    fun encode(bytes: ByteArray): String = java.util.Base64.getUrlEncoder().withoutPadding().encodeToString(bytes)
    fun decode(value: String): ByteArray = java.util.Base64.getUrlDecoder().decode(value)
    fun sha256Hex(bytes: ByteArray): String = MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }
    fun canonicalRequest(method: String, path: String, timestamp: String, nonce: String, body: ByteArray): String =
        "$method\n$path\n$timestamp\n$nonce\n${sha256Hex(body)}"
    fun shortWindowDuplicateKey(sbnKey: String, wechatUserId: Int, preview: Preview): String =
        "$wechatUserId\n$sbnKey\n${previewJson(preview)}"
    fun notificationIdentity(sbnKey: String, wechatUserId: Int, postTime: Long, preview: Preview): String =
        "$wechatUserId\n$sbnKey\n$postTime\n${previewJson(preview)}"
    fun previewJson(value: Preview): String = "{\"sender\":${json(value.sender)},\"body\":${json(value.body)},\"type\":${json(value.type)}}"
    fun envelopeJson(value: Envelope): String = "{\"alg\":${json(value.alg)},\"kid\":${json(value.kid)},\"iv\":${json(value.iv)},\"aad\":${json(value.aad)},\"ct\":${json(value.ct)}}"
    fun assetJson(value: MediaAsset): String = "{\"id\":${json(value.id)},\"kind\":${json(value.kind)},\"mimeType\":${json(value.mimeType)},\"width\":${value.width},\"height\":${value.height},\"envelope\":${envelopeJson(value.envelope)}}"
    private fun json(value: String): String = buildString(value.length + 2) {
        append('"')
        value.forEach { character ->
            when (character) {
                '"' -> append("\\\"")
                '\\' -> append("\\\\")
                '\b' -> append("\\b")
                '\u000C' -> append("\\f")
                '\n' -> append("\\n")
                '\r' -> append("\\r")
                '\t' -> append("\\t")
                else -> if (character.code < 0x20) append("\\u${character.code.toString(16).padStart(4, '0')}") else append(character)
            }
        }
        append('"')
    }
}
