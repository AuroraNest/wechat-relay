package com.aurora.wechatrelay.probe

import android.content.Context
import android.os.Bundle
import android.os.Parcelable
import java.security.MessageDigest
import java.security.SecureRandom

data class RedactedValue(
    val type: String,
    val length: Int,
    val hash: String?,
)

object Privacy {
    private const val SaltPreference = "probe_privacy"
    private const val SaltKey = "salt_base64"

    fun salt(context: Context): ByteArray {
        val preferences = context.getSharedPreferences(SaltPreference, Context.MODE_PRIVATE)
        val existing = preferences.getString(SaltKey, null)
        if (existing != null) {
            return java.util.Base64.getDecoder().decode(existing)
        }

        val created = ByteArray(32).also(SecureRandom()::nextBytes)
        preferences.edit().putString(SaltKey, java.util.Base64.getEncoder().encodeToString(created)).commit()
        return created
    }

    fun redact(value: Any?, salt: ByteArray): RedactedValue = when (value) {
        null -> RedactedValue("null", 0, null)
        is CharSequence -> RedactedValue(value.javaClass.name, value.length, saltedHash(value.toString(), salt))
        is ByteArray -> RedactedValue(value.javaClass.name, value.size, saltedHash(value, salt))
        is BooleanArray -> RedactedValue(value.javaClass.name, value.size, null)
        is CharArray -> RedactedValue(value.javaClass.name, value.size, null)
        is DoubleArray -> RedactedValue(value.javaClass.name, value.size, null)
        is FloatArray -> RedactedValue(value.javaClass.name, value.size, null)
        is IntArray -> RedactedValue(value.javaClass.name, value.size, null)
        is LongArray -> RedactedValue(value.javaClass.name, value.size, null)
        is ShortArray -> RedactedValue(value.javaClass.name, value.size, null)
        is Boolean, is Number, is Char -> RedactedValue(value.javaClass.name, 1, saltedHash(value.toString(), salt))
        is Array<*> -> RedactedValue(value.javaClass.name, value.size, null)
        is Bundle -> RedactedValue(value.javaClass.name, value.size(), null)
        is Parcelable -> RedactedValue(value.javaClass.name, -1, null)
        else -> RedactedValue(value.javaClass.name, -1, null)
    }

    fun saltedHash(value: String, salt: ByteArray): String = saltedHash(value.toByteArray(Charsets.UTF_8), salt)

    fun saltedHash(value: ByteArray, salt: ByteArray): String {
        val digest = MessageDigest.getInstance("SHA-256")
        digest.update(salt)
        digest.update(value)
        return java.util.Base64.getUrlEncoder().withoutPadding().encodeToString(digest.digest())
    }
}
